# To use this on a new instance of Redash,
# add 'redash.query_runner.mssql_all_datasources' to the array of 'default_query_runners'
# inside /opt/redash/current/redash/settings.py
# and restart the celery workers using `sudo supervisorctl restart all`

import json
import logging
import sys
import uuid

from redash.query_runner import *
from redash.utils import JSONEncoder

logger = logging.getLogger(__name__)

try:
    import pymssql
    enabled = True
except ImportError:
    enabled = False

# from _mssql.pyx ## DB-API type definitions & http://www.freetds.org/tds.html#types ##
types_map = {
    1: TYPE_STRING,
    2: TYPE_BOOLEAN,
    # Type #3 supposed to be an integer, but in some cases decimals are returned
    # with this type. To be on safe side, marking it as float.
    3: TYPE_FLOAT,
    4: TYPE_DATETIME,
    5: TYPE_FLOAT,
}


class MSSQLJSONEncoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, uuid.UUID):
            return str(o)
        return super(MSSQLJSONEncoder, self).default(o)


class SqlServerAllDatasources(BaseSQLQueryRunner):
    noop_query = "SELECT 1"

    @classmethod
    def configuration_schema(cls):
        return {
            "type": "object",
            "properties": {
                "user": {
                    "type": "string"
                },
                "password": {
                    "type": "string"
                },
                "server": {
                    "type": "string",
                    "default": "127.0.0.1"
                },
                "port": {
                    "type": "number",
                    "default": 1433
                },
                "tds_version": {
                    "type": "string",
                    "default": "4.2",
                    "title": "TDS Version"
                },
                "charset": {
                    "type": "string",
                    "default": "ISO-8859-1",
                    "title": "Character Set"
                },
                "db": {
                    "type": "string",
                    "title": "Schema Database Name (for Schema Searching)"
                },
                "master_db": {
                    "type": "string",
                    "title": "Master Database Name (for retrieving datasources)"
                },
                "datasources_query": {
                    "type": "string",
                    "title": "Query to retrieve datasources"
                }
            },
            "required": ["db","master_db","datasources_query"],
            "secret": ["password"]
        }

    @classmethod
    def enabled(cls):
        return enabled

    @classmethod
    def name(cls):
        return "Microsoft SQL Server - Multiple Datasources"

    @classmethod
    def type(cls):
        return "mssql_all_datasources"

    @classmethod
    def annotate_query(cls):
        return False

    def __init__(self, configuration):
        super(SqlServerAllDatasources, self).__init__(configuration)

    def _get_tables(self, schema):
        query = """
        SELECT table_schema, table_name, column_name
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema NOT IN ('guest','INFORMATION_SCHEMA','sys','db_owner','db_accessadmin'
                                  ,'db_securityadmin','db_ddladmin','db_backupoperator','db_datareader'
                                  ,'db_datawriter','db_denydatareader','db_denydatawriter'
                                  );
        """

        results, error = self.run_query_on_datasource(query, None, self.configuration.get('db', ''))

        if error is not None:
            raise Exception("Failed getting schema.")

        results = json.loads(results)

        for row in results['rows']:
            if row['table_schema'] != self.configuration['db']:
                table_name = u'{}.{}'.format(row['table_schema'], row['table_name'])
            else:
                table_name = row['table_name']

            if table_name not in schema:
                schema[table_name] = {'name': table_name, 'columns': []}

            schema[table_name]['columns'].append(row['column_name'])

        return schema.values()

    def run_query(self, query, user):
        connection = None
        datasources_json, error = self.run_query_on_datasource(self.configuration.get('datasources_query', ''), user, self.configuration.get('master_db', ''))

        if error is not None:
            raise Exception("Failed retrieving datasources.")

        total_rows = []
        datasources = json.loads(datasources_json)

        for d in datasources['rows']:
            try:
                datasource = d['datasource']
                results_json, error = self.run_query_on_datasource(query, user, datasource)
                results = json.loads(results_json)
                total_rows.extend(results['rows'])
            except Exception as e:
                logging.exception("message")

        results['rows'] = total_rows

        return json.dumps(results, cls=MSSQLJSONEncoder), error

    def run_query_on_datasource(self, query, user, db):
        try:
            server = self.configuration.get('server', '')
            user = self.configuration.get('user', '')
            password = self.configuration.get('password', '')
            port = self.configuration.get('port', 1433)
            tds_version = self.configuration.get('tds_version', '4.2')
            charset = self.configuration.get('charset', 'ISO-8859-1')

            if port != 1433:
                server = server + ':' + str(port)

            connection = pymssql.connect(server=server, user=user, password=password, database=db, tds_version=tds_version, charset=charset)

            if isinstance(query, unicode):
                query = query.encode(charset)

            cursor = connection.cursor()
            logger.debug("SqlServerAllDatasources running query: %s", query)

            cursor.execute(query)
            data = cursor.fetchall()

            if cursor.description is not None:
                columns = self.fetch_columns([(i[0], types_map.get(i[1], None)) for i in cursor.description])
                rows = [dict(zip((c['name'] for c in columns), row)) for row in data]

                data = {'columns': columns, 'rows': rows}
                json_data = json.dumps(data, cls=MSSQLJSONEncoder)
                error = None
            else:
                error = "No data was returned."
                json_data = None

            cursor.close()
        except pymssql.Error as e:
            try:
                # Query errors are at `args[1]`
                error = e.args[1]
            except IndexError:
                # Connection errors are `args[0][1]`
                error = e.args[0][1]
            json_data = None
        except KeyboardInterrupt:
            connection.cancel()
            error = "Query cancelled by user."
            json_data = None
        except Exception as e:
            raise sys.exc_info()[1], None, sys.exc_info()[2]
        finally:
            if connection:
                connection.close()

        return json_data, error

register(SqlServerAllDatasources)
