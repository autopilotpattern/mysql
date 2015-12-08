# triton-mysql

MySQL designed for container-native deployment on Joyent's Triton platform.

### Architecture

We have a primary (`mysql_primary`) and replicas (`mysql`) as separate `docker-compose` services. The replicas ask Consul (via Containerbuddy) for the primary's IP and self-configure replication with the primary in their `onStart` handler. The replicas' `onChange` handler will move replication to a new master if one is created.


### Configuration

Pass these variables in your environment or via an .env file.

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

On Triton we have the bad-assness of ZFS so we don't need to mess around with data volumes.

### Usage Against an Existing Database

If you start your MySQL container instance with a data directory that already contains a database (specifically, a mysql subdirectory), the pre-existing database won't be changed in any way.
