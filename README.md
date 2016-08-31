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
- `triton-mysql.py`: a small Python application that ContainerPilot will call into to do the heavy lifting of bootstrapping MySQL.

When a new MySQL node is started, ContainerPilot's `preStart` handler will call into `triton-mysql.py`.


### Bootstrapping via `preStart` handler

`preStart` (formerly `onStart`) runs and must exit cleanly before the main application is started.

The first thing the `triton-mysql.py` application does is to ask Consul whether a primary node exists. If not, the application will atomically mark the node as primary in Consul and then bootstrap the node as a new primary. Bootstrapping a primary involves setting up users (root, default, and replication), and creating a default schema. Once the primary bootstrap process is complete, it will use `xtrabackup` to create a backup and upload it to Manta. The application then writes a TTL key to Consul which will tell us when next to run a backup, and a non-expiring key that records the path on Manta where the most recent backup was stored.

If a primary already exists, then the application will ask Consul for the path to the most recent backup snapshot and download it and the most recent binlog. The application will then ask Consul for the IP address of the primary and set up replication from that primary before allowing the new replica to join the cluster.

Replication in this architecture uses [Global Transaction Identifiers (GTID)](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids.html) so that replicas can autoconfigure their position within the binlog.

### Maintenance via `health` handler

The ContainerPilot `health` handler calls `triton-mysql.py health`. If the node is a replica, it merely checks the health of the instance by making a `SELECT 1` against the database and exiting with exit code 0 (which ContainerPilot then interprets as a message to send a heartbeat to Consul).

If the node is primary, the handler will ask Consul if the TTL key for backups have expired or whether the binlog has been rotated. If either case is true, the application will create a new snapshot, upload it to Manta, and write the appropriate keys to Consul to tell replicas where to find the backup.

### Failover via `onChange` handler

The ContainerPilot configuration for replicas watches for changes to the primary. If the primary becomes unhealthy or updates its IP address, the `onChange` handler will fire and call into `triton-mysql.py`. Replicas will all attempt to become the new primary by writing a lock into Consul. Only one replica will receive the lock and become the new primary. The new primary will reload its configuration to start writing its heartbeats as primary, while the other replicas will update their replication configuration to start replicating from the new primary.

### Promotion of replica to primary

With the automatic failover described above, manually promoting a replica to a primary is a simple matter of updating the flag we've set in Consul and then allowing the ContainerPilot `onChange` handlers to update the replication topology for each node independently.

- Update the `mysql-primary` key in Consul to match the new primary's short-form container ID (i.e. its hostname).
- The `onChange` handlers on the other replicas will automatically detect the change in what node is the primary, and will execute `CHANGE MASTER` to change their replication source to the new primary.
- At any point after this, we use `docker exec` to run `STOP SLAVE` on the new primary and can tear down the old primary if it still exists.

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
- `LOG_LEVEL`: will set the logging level of the `triton-mysql.py` application. It defaults to `DEBUG` and uses the Python stdlib [log levels](https://docs.python.org/2/library/logging.html#levels). In production you'll want this to be at `INFO` or above.
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

These variables *may* be passed but it's not recommended to do this. Instead we'll set a one-time root password during DB initialization; the password will be dropped into the logs. Security can be improved by using a key management system in place of environment variables. The constructor for the `MySQLNode` class would be a good place to hook in this behavior, which is out-of-scope for this demonstration.

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
