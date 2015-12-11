# triton-mysql

MySQL designed for container-native deployment on Joyent's Triton platform.

### Running a new cluster

Starting a new cluster is easy. Just run `docker-compose up -d` and in a few moments you'll have a running MySQL primary. Both the primary and replicas are described as a single `docker-compose` service. During startup, [Containerbuddy](http://containerbuddy.io) will ask Consul if an existing primary has been created. If not, the node will initialize as a new primary and all future nodes will self-configure replication with the primary in their `onStart` handler.

Run `docker-compose scale mysql=2` to add a replica (or more than one!). Replication in this architecture uses [Global Transaction Idenitifers (GTID)](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids.html) rather than binlog positioning as this allows replicas to autoconfigure their position within the binlog. A primary that has the entire execution history can bootstrap a replica with no additional work. The replicas' `onChange` handler will automatically move replication to a new primary if one is created.

So long as we're working with a new cluster that has never rotated the binlog on the primary (and where there's not too much data yet), the new replicas will be able to catch up to the primary without further intervention. But if you're bringing up a replica in an existing cluster, you'll need to try the following workflow.

### Adding a replica to an existing cluster

A primary that has rotated the binlog or simply has a large binlog will be impractical to use to bootstrap replication without copying data first. In this case we're going to [copy the MySQL data directory](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids-failover.html) to the new replica's file system.

In order to safely snapshot MySQL, we need to prevent new writes. In order to avoid downtime for the application, we recommend using one of the other replicas as a source for the data directory. The process that's been automated here is as follows:

- Write a "lock" key to Consul which the replica will look for so that it doesn't complete replication setup until it has the data transfered from the source.
- Start a new replica node. The new replica will see there's an existing primary, but will pause the setup process until the lock key has been removed from Consul.
- Use `docker exec` to run `STOP SLAVE` on the source node.
- Copy the data directory from the source node to the new replica.
- Use `docker exec` to run `START SLAVE` on the source node.
- Remove the lock key from Consul.
- The new replica will see the removed key and continue replication setup, looking for the primary in Consul and running `CHANGE MASTER` and `START SLAVE`.


### Promoting a replica to primary

Taking an existing replica and making it a primary for the cluster is a simple matter of updating the flag we've set in Consul and then allowing the Containerbuddy `onChange` handlers to update the replication topology for each node independently.

- Update the `mysql-primary` in Consul.
- The `onChange` handlers on the other replicas will automatically detect the change in what node is the primary, and will execute `CHANGE MASTER` to change their replication source to the new primary.
- At any point after this, we use `docker exec` to run `STOP SLAVE` on the new primary and can tear down the old primary if it still exists.


### Configuration

Pass these variables in your environment or via an `.env` file.

- `MYSQL_USER`: this user will be set up as the default non-root user on the node
- `MYSQL_PASSWORD`: this user will be set up as the default non-root user on the node

These variables are optional but you most likely want them:

- `MYSQL_REPL_USER`: this user will be used on all instances to set up MySQL replication. If not set, then replication will not be set up on the replicas.
- `MYSQL_REPL_PASSWORD`: this password will be used on all instances to set up MySQL replication. If not set, then replication will not be set up on the replicas.
- `MYSQL_DATABASE`: create this database on startup if it doesn't already exist. The `MYSQL_USER` user will be granted superuser access to that DB.

These variables *may* be passed but it's not recommended to do this. Instead we'll set a one-time root password during DB initialization; the password will be dropped into the logs.

- `MYSQL_RANDOM_ROOT_PASSWORD`: defaults to "yes"
- `MYSQL_ONETIME_PASSWORD`: defaults to "yes"
- `MYSQL_ROOT_PASSWORD`: default to being unset

These variables will be written to `/etc/my.cnf`.

- `INNODB_BUFFER_POOL_SIZE`: innodb_buffer_pool_size

### Where to Store Data

On Triton there's not need to use data volumes because the performance hit you normally take with overlay file systems in Linux doesn't happen with ZFS.

### Usage Against an Existing Database

If you start your MySQL container instance with a data directory that already contains a database (specifically, a mysql subdirectory), the pre-existing database won't be changed in any way.
