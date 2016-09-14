# Autopilot Pattern MySQL

MySQL designed for automated operation using the [Autopilot Pattern](http://autopilotpattern.io/). This repo serves as a blueprint demonstrating the pattern -- automatically setting up replication, backups, and failover without human intervention.

[![DockerPulls](https://img.shields.io/docker/pulls/autopilotpattern/mysql.svg)](https://registry.hub.docker.com/u/autopilotpattern/mysql/)
[![DockerStars](https://img.shields.io/docker/stars/autopilotpattern/mysql.svg)](https://registry.hub.docker.com/u/autopilotpattern/mysql/)

---

## Architecture

A running cluster includes the following components:

- [MySQL](https://dev.mysql.com/): we're using MySQL5.6 via [Percona Server](https://www.percona.com/software/mysql-database/percona-server), and [`xtrabackup`](https://www.percona.com/software/mysql-database/percona-xtrabackup) for running hot snapshots.
- [ContainerPilot](https://www.joyent.com/containerpilot): included in our MySQL containers to orchestrate bootstrap behavior and coordinate replication using keys and checks stored in Consul in the `preStart`, `health`, and `onChange` handlers.
- [Consul](https://www.consul.io/): is our service catalog that works with ContainerPilot and helps coordinate service discovery, replication, and failover
- [Manta](https://www.joyent.com/object-storage): the Joyent object store, for securely and durably storing our MySQL snapshots.
- `manage.py`: a small Python application that ContainerPilot's lifecycle hooks will call to bootstrap MySQL, perform health checks, manage replication setup, and perform coordinated failover.

The lifecycle of a MySQL container is managed by 4 lifecycle hooks in the `manage.py` application: `pre_start`, `health`, `on_change`, and `snapshot_task`.

### Bootstrapping via `pre_start` handler

When a container is started ContainerPilot will run the `manage.py pre_start` function, which must exit cleanly before the MySQL server will be started. See [ContainerPilot's `preStart` action docs](https://www.joyent.com/containerpilot/docs/start-stop) for how this is triggered.

Once `pre_start` has gathered its configuration from the environment it verifies whether this instance has been previously started. If not, it asks Consul whether a snapshot image for the database exists on Manta. If so, it will download the snapshot and initialize the database from that snapshot using the Percona `xtrabackup` tool. If not, we perform the initial MySQL setup via `mysql_install_db`. Note that we're assuming that the first instance is launched and allowed to initialize before we bring up other instances, but this only applies the very first time we bring up the cluster. Also note that at this time the MySQL server is not yet running and so we can't complete replication setup.


### Maintenance via `health` handler

The ContainerPilot `health` handler calls `manage.py health` periodically. The behavior of this handler depends on whether the instance is a primary, a replica, or hasn't yet been initialized as either. See [ContainerPilot's service health check docs](https://www.joyent.com/containerpilot/docs/health) for how to use this in your own application.

The `health` function first checks whether this instance has been previously initialized (via checking a lock file on disk). If not, it'll check if a primary has been registered with Consul. If not, the handler will attempt to obtain a lock in Consul marking it as the primary. If the lock fails (perhaps because we're bringing up multiple hosts at once and another has obtained the lock), then we'll exit and retry on the next call of the `health` function. If the lock succeeds this node is marked the primary and we'll bootstrap the rest of the MySQL application. This includes creating a default user, replication user, and default schema, as well as resetting the root password, and writing a snapshot of the initialized DB to Manta (where another instance can download it during `pre_start`).

If this is the first pass thru the health check and a primary has already been registered in Consul, then the handler will set up replication to the primary. Replication in this architecture uses [Global Transaction Identifiers (GTID)](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids.html) so that replicas can autoconfigure their position within the binlog.

If this isn't the first pass thru the health check and we've already set up this node, then the health check can continue. If this node is the primary, the handler will execute a `SELECT 1` against the database to make sure its up and then renew the session in Consul indicating it is the primary. If the node is a replica, the handler will make sure that `SHOW SLAVE STATUS` returns a value. When the handler exits successfully, ContainerPilot will send a heartbeat with TTL to Consul indicating that the node is still healthy.


### Failover via `on_change` handler

The ContainerPilot configuration for replicas watches for changes to the primary. If the primary becomes unhealthy or updates its IP address, ContainerPilot will call `manage.py on_change` to perform failover. See [ContainerPilot's `onChange` docs](https://www.joyent.com/containerpilot/docs/lifecycle#while-the-container-is-running) for how changes are identified.

All remaining instances will receive the `on_change` handler call so there is a process of coordination involved to ensure that only one instance actually attempts to perform the failover. The first step is to check if there is a healthy primary! Because the `on_change` handlers are polling asynchronously it's entirely possible for another instance to have completed failover before a given instance runs its `on_change` handler. In this case, the node will either mark itself as primary (if it was assigned to be the primary by the failover node, see below) or set up replication to the new primary.

Once we've determined that the node should attempt a failover it tries to obtain a lock in Consul. If the failover lock fails this means another node is going to run the failover and this handler should wait for it to be completed. Once the lock has been removed the handler will either mark itself as the new primary and reload its ContainerPilot configuration to match, or will realize its a replica and quietly exit.

If the failover lock succeeds then this node will run the failover, and it will do so via [`mysqlrpladmin failover`](http://dev.mysql.com/doc/mysql-utilities/1.6/en/mysqlrpladmin.html). The handler gets a list of healthy MySQL instances from Consul and passes these as candidates for primary to the `mysqlrpladmin` tool. This tool will stop replication on all candidates, determine which candidate is the best one to use as the primary, ensure that all nodes have the same transactions, and then set up replication for all replicas to the new primary. See [this blog post by Percona](https://www.percona.com/blog/2014/06/27/failover-mysql-utilities-part1-mysqlrpladmin/) for more details on how this failover step works.

Once the failover is complete, the failover node will release the lock after its next pass through the health check. This ensures that if the failover node marked itself as the primary that other replicas don't attempt to failover spuriously because there's no healthy primary.


### Backups in the `snapshot_task`

If the node is primary, the handler will ask Consul if the TTL key for backups have expired or whether the binlog has been rotated. If either case is true, the application will create a new snapshot, upload it to Manta, and write the appropriate keys to Consul to tell replicas where to find the backup. See [ContainerPilot's period task docs](https://www.joyent.com/containerpilot/docs/tasks) to learn how the recurring snapshot task is configured.


---

### Guarantees

It's very important to note that the failover process described above prevents data corruption by ensuring that all replicas have the same set of transactions before continuing. But because MySQL replication is asynchronous it cannot protect against data *loss*. It's entirely possible for the primary to fail without any replica having received its last transactions. This is an inherent limitation of MySQL asynchronous replication and you must architect your application to take this into account.

Also note that during failover, the MySQL cluster is unavailable for writes. Any client application should be using ContainerPilot or some other means to watch for changes to the `mysql-primary` service and halt writes until the failover is completed. Writes sent to a failed primary during failover will be lost!


### Determining if a node is primary

In most the handlers described above, there is a need to determine whether the node executing the handler thinks it is the current primary. This is determined as follows:

- Ask `mysqld` for replication status. If the node is replicating from an instance then it is a replica, and if it has replicas then it is a primary. If neither, continue to the next step.
- Ask Consul for a health instance of the `mysql-primary` service. If Consul returns an IP that matches this node, then it is primary. If Consul returns an IP that doesn't match this node, then it is a replica. If neither, then we cannot determine whether the node is primary or replica and it is marked "unassigned."

Note that this determines only whether this node *thinks* it is the primary instance. During initialization (in `health` checks) an unassigned node will try to elect itself the new primary. During failover, if a node is a replica then it will also check to see if the primary that it found is actually healthy.


---

## Running the cluster

Starting a new cluster is easy once you have [your `_env` file set with the configuration details](#configuration), **just run `docker-compose up -d` and in a few moments you'll have a running MySQL primary**. Both the primary and replicas are described as a single `docker-compose` service. During startup, [ContainerPilot](http://containerpilot.io) will ask Consul if an existing primary has been created. If not, the node will initialize as a new primary and all future nodes will self-configure replication with the primary in their `preStart` handler.

**Run `docker-compose scale mysql=2` to add a replica (or more than one!)**. The replicas will automatically configure themselves to to replicate from the primary and will register themselves in Consul as replicas once they're ready.

### Configuration

Pass these variables via an `_env` file. The included `setup.sh` can be used to test your Docker and Triton environment, and to encode the Manta SSH key in the `_env` file.

- `MYSQL_USER`: this user will be set up as the default non-root user on the node
- `MYSQL_PASSWORD`: this user will be set up as the default non-root user on the node
- `MANTA_URL`: the full Manta endpoint URL. (ex. `https://us-east.manta.joyent.com`)
- `MANTA_USER`: the Manta account name.
- `MANTA_SUBUSER`: the Manta subuser account name, if any.
- `MANTA_ROLE`: the Manta role name, if any.
- `MANTA_KEY_ID`: the MD5-format ssh key id for the Manta account/subuser (ex. `1a:b8:30:2e:57:ce:59:1d:16:f6:19:97:f2:60:2b:3d`); the included `setup.sh` will encode this automatically
- `MANTA_PRIVATE_KEY`: the private ssh key for the Manta account/subuser; the included `setup.sh` will encode this automatically
- `MANTA_BUCKET`: the path on Manta where backups will be stored. (ex. `/myaccount/stor/triton-mysql`); the bucket must already exist and be writeable by the `MANTA_USER`/`MANTA_PRIVATE_KEY`

These variables are optional but you most likely want them:

- `MYSQL_REPL_USER`: this user will be used on all instances to set up MySQL replication. If not set, then replication will not be set up on the replicas.
- `MYSQL_REPL_PASSWORD`: this password will be used on all instances to set up MySQL replication. If not set, then replication will not be set up on the replicas.
- `MYSQL_DATABASE`: create this database on startup if it doesn't already exist. The `MYSQL_USER` user will be granted superuser access to that DB.
- `LOG_LEVEL`: will set the logging level of the `manage.py` application. It defaults to `DEBUG` and uses the Python stdlib [log levels](https://docs.python.org/2/library/logging.html#levels). The `DEBUG` log level is extremely verbose -- in production you'll want this to be at `INFO` or above.
- `CONSUL` is the hostname for the Consul instance(s). Defaults to `consul`.

The following variables control the names of keys written to Consul. They are optional with sane defaults, but if you are using Consul for many other services you might have requirements to namespace keys:

- `PRIMARY_KEY`: The key used to record a lock on what node is primary. (Defaults to `mysql-primary`.)
- `STANDBY_KEY`: The key used to record a lock on what node is standby. (Defaults to `mysql-standby`.)
- `BACKUP_TTL_KEY`: The name of the service that the backup TTL will be associated with. (Defaults to `mysql-backup-run`.)
- `LAST_BACKUP_KEY`: The key used to store the path to the most recent backup. (Defaults to `mysql-last-backup`.)
- `LAST_BINLOG_KEY`: The key used to store the filename of the most recent binlog file on the primary. (Defaults to `mysql-last-binlog`.)
- `BACKUP_NAME`: The name of the backup file that's stored on Manta, with optional [strftime](https://docs.python.org/2/library/time.html#time.strftime) directives. (Defaults to `mysql-backup-%Y-%m-%dT%H-%M-%SZ`.)
- `BACKUP_TTL`: Time in seconds to wait between backups. (Defaults to `86400`, or 24 hours.)
- `SESSION_NAME`: The name used for session locks. (Defaults to `mysql-primary-lock`.)

These variables *may* be passed but it's not recommended to do this. Instead we'll set a one-time root password during DB initialization; the password will be dropped into the logs. Security can be improved by using a key management system in place of environment variables. The constructor for the `Node` class in `manage.py` would be a good place to hook in this behavior, which is out-of-scope for this demonstration.

- `MYSQL_RANDOM_ROOT_PASSWORD`: defaults to "yes"
- `MYSQL_ONETIME_PASSWORD`: defaults to "yes"
- `MYSQL_ROOT_PASSWORD`: default to being unset

These variables will be written to `/etc/my.cnf`.

- `INNODB_BUFFER_POOL_SIZE`: innodb_buffer_pool_size


Environment variables are expanded automatically.
This allows you to [use environment variables](https://docs.docker.com/compose/compose-file/#/environment) from the machine where `docker compose` runs.
Example:

```
# local-compose.yml
mysql:
  environment:
    USER:
```

```
# _env
MANTA_BUCKET=/companyaccount/stor/developers/${USER}/backups
```

### Where to store data

This pattern automates the data management and makes container effectively stateless to the Docker daemon and schedulers. This is designed to maximize convenience and reliability by minimizing the external coordination needed to manage the database. The use of external volumes (`--volumes-from`, `-v`, etc.) is not recommended.

On Triton, there's no need to use data volumes because the performance hit you normally take with overlay file systems in Linux doesn't happen with ZFS.

### Using an existing database

If you start your MySQL container instance with a data directory that already contains a database (specifically, a mysql subdirectory), the pre-existing database won't be changed in any way.
