from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Tuple, Union

import mariadb
from mariadb.constants import FIELD_TYPE
from pymysql.constants import ER
from pymysql.converters import escape_string

import frappe
from frappe.database.database import Database
from frappe.database.mariadb.schema import MariaDBTable
from frappe.utils import UnicodeWithAttrs, get_datetime, get_table_name

if TYPE_CHECKING:
	from mariadb import ConnectionPool

_SITE_POOLS = defaultdict(frappe._dict)
_MAX_POOL_SIZE = 64
_POOL_SIZE = 4

# _POOL_SIZE is selected "arbitrarily" to avoid overloading the server and being mindful of multitenancy
# init size of connection pool will be _POOL_SIZE for each site. Replica setups will have separate pool.
# This means each site with a replica setup can have 2 active pools of size _POOL_SIZE each. Each pool may
# expand up to _MAX_POOL_SIZE as per requirement. This cannot be a function of @@global.max_connections,
# no. of sites since there may be multiple processes holding connections; and this defines the size for each
# of those processes/workers. Check MariaDBConnectionUtil for connection & pool management.


class MariaDBExceptionUtil:
	ProgrammingError = mariadb.ProgrammingError
	TableMissingError = mariadb.ProgrammingError
	OperationalError = mariadb.OperationalError
	InternalError = mariadb.InternalError
	SQLError = mariadb.ProgrammingError
	DataError = mariadb.DataError

	@staticmethod
	def is_deadlocked(e: mariadb.Error) -> bool:
		return e.errno == ER.LOCK_DEADLOCK

	@staticmethod
	def is_timedout(e: mariadb.Error) -> bool:
		return e.errno == ER.LOCK_WAIT_TIMEOUT

	@staticmethod
	def is_table_missing(e: mariadb.Error) -> bool:
		return e.errno == ER.NO_SUCH_TABLE

	@staticmethod
	def is_missing_table(e: mariadb.Error) -> bool:
		return MariaDBDatabase.is_table_missing(e)

	@staticmethod
	def is_missing_column(e: mariadb.Error) -> bool:
		return e.errno == ER.BAD_FIELD_ERROR

	@staticmethod
	def is_duplicate_fieldname(e: mariadb.Error) -> bool:
		return e.errno == ER.DUP_FIELDNAME

	@staticmethod
	def is_duplicate_entry(e: mariadb.Error) -> bool:
		return e.errno == ER.DUP_ENTRY

	@staticmethod
	def is_access_denied(e: mariadb.Error) -> bool:
		return e.errno == ER.ACCESS_DENIED_ERROR

	@staticmethod
	def cant_drop_field_or_key(e: mariadb.Error) -> bool:
		return e.errno == ER.CANT_DROP_FIELD_OR_KEY

	@staticmethod
	def is_syntax_error(e: mariadb.Error) -> bool:
		return e.errno == ER.PARSE_ERROR

	@staticmethod
	def is_data_too_long(e: mariadb.Error) -> bool:
		return e.errno == ER.DATA_TOO_LONG

	@staticmethod
	def is_primary_key_violation(e: mariadb.Error) -> bool:
		return (
			MariaDBDatabase.is_duplicate_entry(e)
			and "PRIMARY" in e.errmsg
			and isinstance(e, mariadb.IntegrityError)
		)

	@staticmethod
	def is_unique_key_violation(e: mariadb.Error) -> bool:
		return (
			MariaDBDatabase.is_duplicate_entry(e)
			and "Duplicate" in e.errmsg
			and isinstance(e, mariadb.IntegrityError)
		)


class MariaDBConnectionUtil:
	def get_connection(self):
		"""Return MariaDB connection object.

		If frappe.conf.disable_database_connection_pooling is set, return a new connection
		object and close existing pool if exists. Else, return a connection from the pool.
		"""
		# get pooled connection
		global _SITE_POOLS

		if frappe.conf.disable_database_connection_pooling:
			self.close_connection_pools()
			return self.create_connection()

		is_read_only_conn = hasattr(frappe.local, "primary_db")

		if frappe.local.site not in _SITE_POOLS:
			site_pool = self.create_connection_pool(read_only=is_read_only_conn)
		else:
			site_pool = self.get_connection_pool(read_only=is_read_only_conn)

		try:
			conn = site_pool.get_connection()
		except mariadb.PoolError:
			# PoolError is raised when the pool is exhausted
			conn = self.create_connection()
			try:
				site_pool.add_connection(conn)
				# log this via frappe.logger & continue - site needs bigger pool...over _POOL_SIZE
			except mariadb.PoolError:
				# PoolError is raised when size limit is reached
				# log this via frappe.logger & continue - site needs a much bigger pool...over _MAX_POOL_SIZE
				pass

		return conn

	def close_connection_pools(self):
		if frappe.local.site in _SITE_POOLS:
			pools = _SITE_POOLS[frappe.local.site]
			for pool in pools.values():
				try:
					pool.close()
				except Exception:
					pass
			_SITE_POOLS.pop(frappe.local.site, None)

	def get_pool_name(self, read_only=False) -> str:
		pool_type = "read-only" if read_only else "default"
		return f"{frappe.local.site}-{pool_type}"

	def get_connection_pool(self, read_only=False) -> "ConnectionPool":
		"""Return MariaDB connection pool object.

		If `read_only` is True, return a read only pool.
		"""
		return _SITE_POOLS[frappe.local.site]["read_only" if read_only else "default"]

	def create_connection_pool(self, read_only=False):
		pool = mariadb.ConnectionPool(
			pool_name=self.get_pool_name(read_only=read_only),
			pool_size=_MAX_POOL_SIZE,
			pool_reset_connection=False,
		)
		pool.set_config(**self.get_connection_settings())

		for _ in range(_POOL_SIZE):
			pool.add_connection()

		if read_only:
			_SITE_POOLS[frappe.local.site].read_only = pool
		else:
			_SITE_POOLS[frappe.local.site].default = pool

		return pool

	def create_connection(self):
		return mariadb.connect(**self.get_connection_settings())

	def get_connection_settings(self) -> Dict:
		conn_settings = {
			"host": self.host,
			"user": self.user,
			"password": self.password,
			"database": self.user,
			"converter": {
				FIELD_TYPE.NEWDECIMAL: float,
				FIELD_TYPE.DATETIME: get_datetime,
				UnicodeWithAttrs: escape_string,
			},
		}

		if self.port:
			conn_settings["port"] = int(self.port)

		if frappe.conf.local_infile:
			conn_settings["local_infile"] = frappe.conf.local_infile

		if frappe.conf.db_ssl_ca and frappe.conf.db_ssl_cert and frappe.conf.db_ssl_key:
			ssl_params = {
				"ssl": True,
				"ssl_ca": frappe.conf.db_ssl_ca,
				"ssl_cert": frappe.conf.db_ssl_cert,
				"ssl_key": frappe.conf.db_ssl_key,
			}
			conn_settings.update(ssl_params)
		return conn_settings


class MariaDBDatabase(MariaDBConnectionUtil, MariaDBExceptionUtil, Database):
	REGEX_CHARACTER = "regexp"

	# NOTE: using a very small cache - as during backup, if the sequence was used in anyform,
	# it drops the cache and uses the next non cached value in setval query and
	# puts that in the backup file, which will start the counter
	# from that value when inserting any new record in the doctype.
	# By default the cache is 1000 which will mess up the sequence when
	# using the system after a restore.
	# issue link: https://jira.mariadb.org/browse/MDEV-21786
	SEQUENCE_CACHE = 50

	def setup_type_map(self):
		self.db_type = "mariadb"
		self.type_map = {
			"Currency": ("decimal", "21,9"),
			"Int": ("int", "11"),
			"Long Int": ("bigint", "20"),
			"Float": ("decimal", "21,9"),
			"Percent": ("decimal", "21,9"),
			"Check": ("int", "1"),
			"Small Text": ("text", ""),
			"Long Text": ("longtext", ""),
			"Code": ("longtext", ""),
			"Text Editor": ("longtext", ""),
			"Markdown Editor": ("longtext", ""),
			"HTML Editor": ("longtext", ""),
			"Date": ("date", ""),
			"Datetime": ("datetime", "6"),
			"Time": ("time", "6"),
			"Text": ("text", ""),
			"Data": ("varchar", self.VARCHAR_LEN),
			"Link": ("varchar", self.VARCHAR_LEN),
			"Dynamic Link": ("varchar", self.VARCHAR_LEN),
			"Password": ("text", ""),
			"Select": ("varchar", self.VARCHAR_LEN),
			"Rating": ("decimal", "3,2"),
			"Read Only": ("varchar", self.VARCHAR_LEN),
			"Attach": ("text", ""),
			"Attach Image": ("text", ""),
			"Signature": ("longtext", ""),
			"Color": ("varchar", self.VARCHAR_LEN),
			"Barcode": ("longtext", ""),
			"Geolocation": ("longtext", ""),
			"Duration": ("decimal", "21,9"),
			"Icon": ("varchar", self.VARCHAR_LEN),
			"Phone": ("varchar", self.VARCHAR_LEN),
			"Autocomplete": ("varchar", self.VARCHAR_LEN),
			"JSON": ("json", ""),
		}

	def get_database_size(self):
		"""'Returns database size in MB"""
		db_size = self.sql(
			"""
			SELECT `table_schema` as `database_name`,
			SUM(`data_length` + `index_length`) / 1024 / 1024 AS `database_size`
			FROM information_schema.tables WHERE `table_schema` = %s GROUP BY `table_schema`
			""",
			self.db_name,
			as_dict=True,
		)

		return db_size[0].get("database_size")

	@staticmethod
	def escape(s, percent=True):
		"""Excape quotes and percent in given string."""
		# Update: We've scrapped PyMySQL in favour of MariaDB's official Python client
		# Also, given we're promoting use of the PyPika builder via frappe.qb, the use
		# of this method should be limited.

		# pymysql expects unicode argument to escape_string with Python 3
		s = frappe.as_unicode(escape_string(frappe.as_unicode(s)), "utf-8").replace("`", "\\`")

		# NOTE separating % escape, because % escape should only be done when using LIKE operator
		# or when you use python format string to generate query that already has a %s
		# for example: sql("select name from `tabUser` where name=%s and {0}".format(conditions), something)
		# defaulting it to True, as this is the most frequent use case
		# ideally we shouldn't have to use ESCAPE and strive to pass values via the values argument of sql
		if percent:
			s = s.replace("%", "%%")

		return "'" + s + "'"

	# column type
	@staticmethod
	def is_type_number(code):
		return code == mariadb.NUMBER

	@staticmethod
	def is_type_datetime(code):
		return code == mariadb.DATETIME

	def rename_table(self, old_name: str, new_name: str) -> Union[List, Tuple]:
		old_name = get_table_name(old_name)
		new_name = get_table_name(new_name)
		return self.sql(f"RENAME TABLE `{old_name}` TO `{new_name}`")

	def describe(self, doctype: str) -> Union[List, Tuple]:
		table_name = get_table_name(doctype)
		return self.sql(f"DESC `{table_name}`")

	def change_column_type(
		self, doctype: str, column: str, type: str, nullable: bool = False
	) -> Union[List, Tuple]:
		table_name = get_table_name(doctype)
		null_constraint = "NOT NULL" if not nullable else ""
		return self.sql_ddl(f"ALTER TABLE `{table_name}` MODIFY `{column}` {type} {null_constraint}")

	def create_auth_table(self):
		self.sql_ddl(
			"""create table if not exists `__Auth` (
				`doctype` VARCHAR(140) NOT NULL,
				`name` VARCHAR(255) NOT NULL,
				`fieldname` VARCHAR(140) NOT NULL,
				`password` TEXT NOT NULL,
				`encrypted` INT(1) NOT NULL DEFAULT 0,
				PRIMARY KEY (`doctype`, `name`, `fieldname`)
			) ENGINE=InnoDB ROW_FORMAT=DYNAMIC CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
		)

	def create_global_search_table(self):
		if not "__global_search" in self.get_tables():
			self.sql(
				"""create table __global_search(
				doctype varchar(100),
				name varchar({0}),
				title varchar({0}),
				content text,
				fulltext(content),
				route varchar({0}),
				published int(1) not null default 0,
				unique `doctype_name` (doctype, name))
				COLLATE=utf8mb4_unicode_ci
				ENGINE=MyISAM
				CHARACTER SET=utf8mb4""".format(
					self.VARCHAR_LEN
				)
			)

	def create_user_settings_table(self):
		self.sql_ddl(
			"""create table if not exists __UserSettings (
			`user` VARCHAR(180) NOT NULL,
			`doctype` VARCHAR(180) NOT NULL,
			`data` TEXT,
			UNIQUE(user, doctype)
			) ENGINE=InnoDB DEFAULT CHARSET=utf8"""
		)

	def create_help_table(self):
		self.sql(
			"""create table help(
				path varchar(255),
				content text,
				title text,
				intro text,
				full_path text,
				fulltext(title),
				fulltext(content),
				index (path))
				COLLATE=utf8mb4_unicode_ci
				ENGINE=MyISAM
				CHARACTER SET=utf8mb4"""
		)

	@staticmethod
	def get_on_duplicate_update(key=None):
		return "ON DUPLICATE key UPDATE "

	def get_table_columns_description(self, table_name):
		"""Returns list of column and its description"""
		return self.sql(
			"""select
			column_name as 'name',
			column_type as 'type',
			column_default as 'default',
			COALESCE(
				(select 1
				from information_schema.statistics
				where table_name="{table_name}"
					and column_name=columns.column_name
					and NON_UNIQUE=1
					limit 1
			), 0) as 'index',
			column_key = 'UNI' as 'unique'
			from information_schema.columns as columns
			where table_name = '{table_name}' """.format(
				table_name=table_name
			),
			as_dict=1,
		)

	def has_index(self, table_name, index_name):
		return self.sql(
			"""SHOW INDEX FROM `{table_name}`
			WHERE Key_name='{index_name}'""".format(
				table_name=table_name, index_name=index_name
			)
		)

	def add_index(self, doctype: str, fields: List, index_name: str = None):
		"""Creates an index with given fields if not already created.
		Index name will be `fieldname1_fieldname2_index`"""
		index_name = index_name or self.get_index_name(fields)
		table_name = get_table_name(doctype)
		if not self.has_index(table_name, index_name):
			self.commit()
			self.sql(
				"""ALTER TABLE `%s`
				ADD INDEX `%s`(%s)"""
				% (table_name, index_name, ", ".join(fields))
			)

	def add_unique(self, doctype, fields, constraint_name=None):
		if isinstance(fields, str):
			fields = [fields]
		if not constraint_name:
			constraint_name = "unique_" + "_".join(fields)

		if not self.sql(
			"""select CONSTRAINT_NAME from information_schema.TABLE_CONSTRAINTS
			where table_name=%s and constraint_type='UNIQUE' and CONSTRAINT_NAME=%s""",
			("tab" + doctype, constraint_name),
		):
			self.commit()
			self.sql(
				"""alter table `tab%s`
					add unique `%s`(%s)"""
				% (doctype, constraint_name, ", ".join(fields))
			)

	def updatedb(self, doctype, meta=None):
		"""
		Syncs a `DocType` to the table
		* creates if required
		* updates columns
		* updates indices
		"""
		res = self.sql("select issingle from `tabDocType` where name=%s", (doctype,))
		if not res:
			raise Exception("Wrong doctype {0} in updatedb".format(doctype))

		if not res[0][0]:
			db_table = MariaDBTable(doctype, meta)
			db_table.validate()

			self.commit()
			db_table.sync()
			self.begin()

	def get_database_list(self):
		return self.sql("SHOW DATABASES", pluck=True)
