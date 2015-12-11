# triton-mysql

MySQL designed for container-native deployment on Joyent's Triton platform.

### Architecture

Both the primary and replicas are described as a single `docker-compose` service. During startup, [Containerbuddy](http://containerbuddy.io) will ask Consul if an existing primary has been created. If not, the node will initialize as a new primary and all future nodes will self-configure replication with the primary in their `onStart` handler.

Replication in this architecture uses [Global Transaction Idenitifers (GTID)](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids.html) rather than binlog positioning as this allows replicas to autoconfigure their position within the binlog. A primary that has the entire execution history can bootstrap a replica with no additional work. The replicas' `onChange` handler will automatically move replication to a new primary if one is created.

A primary that has rotated the binlog or simply has a large binlog will be impractical to use to bootstrap replication without copying data first. In this case, a snapshot of data will be loaded into the new replica and then we will [inject empty transactions](https://dev.mysql.com/doc/refman/5.7/en/replication-gtids-failover.html#replication-gtids-failover-empty) for each transaction in the `gtid_executed` variable from the primary to bring it up to date quickly.


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
