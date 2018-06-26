from . import errors, defines
from .block import Block
from .connection import Connection
from .progress import Progress
from .protocol import ServerPacketTypes
from .util.escape import escape_params
from .util.helpers import chunks


class QueryResult(object):
    def __init__(
            self, packet_generator,
            with_column_types=False, columnar=False):
        self.packet_generator = packet_generator
        self.with_column_types = with_column_types

        self.data = []
        self.columns_with_types = []
        self.columnar = columnar

        super(QueryResult, self).__init__()

    def store(self, packet):
        block = getattr(packet, 'block', None)
        if block is None:
            return

        # Header block contains no rows. Pick columns from it.
        if block.rows:
            if self.columnar:
                columns = block.get_columns()
                if self.data:
                    # Extend corresponding column.
                    for i, column in enumerate(columns):
                        self.data[i] += column
                else:
                    self.data.extend(columns)
            else:
                self.data.extend(block.get_rows())

        elif not self.columns_with_types:
            self.columns_with_types = block.columns_with_types

    def get_result(self):
        for packet in self.packet_generator:
            self.store(packet)

        if self.with_column_types:
            return self.data, self.columns_with_types
        else:
            return self.data


class ProgressQueryResult(QueryResult):
    def __init__(
            self, packet_generator,
            with_column_types=False, columnar=False):
        self.progress_totals = Progress()

        super(ProgressQueryResult, self).__init__(
            packet_generator, with_column_types, columnar
        )

    def store_progress(self, progress_packet):
        self.progress_totals.rows += progress_packet.rows
        self.progress_totals.bytes += progress_packet.bytes
        self.progress_totals.total_rows += progress_packet.total_rows
        return self.progress_totals.rows, self.progress_totals.total_rows

    def __iter__(self):
        return self

    def next(self):
        while True:
            packet = next(self.packet_generator)
            progress_packet = getattr(packet, 'progress', None)
            if progress_packet:
                return self.store_progress(progress_packet)
            else:
                self.store(packet)

    # For Python 3.
    __next__ = next

    def get_result(self):
        # Read all progress packets.
        for _ in self:
            pass

        return super(ProgressQueryResult, self).get_result()


class Client(object):
    def __init__(self, *args, **kwargs):
        self.settings = kwargs.pop('settings', {})

        client_settings = {
            'insert_block_size': self.settings.pop(
                'insert_block_size', defines.DEFAULT_INSERT_BLOCK_SIZE
            )
        }

        self.connection = Connection(*args, **kwargs)
        self.connection.context.settings = self.settings
        self.connection.context.client_settings = client_settings
        super(Client, self).__init__()

    def disconnect(self):
        self.connection.disconnect()

    def receive_result(self, with_column_types=False, progress=False,
                       columnar=False):

        gen = self.packet_generator()

        if progress:
            prog_result = ProgressQueryResult(gen, with_column_types, columnar)
            return prog_result

        else:
            result = QueryResult(gen, with_column_types, columnar)
            return result.get_result()

    def packet_generator(self):
        while True:
            try:
                packet = self.receive_packet()
                if not packet:
                    break

                if packet is True:
                    continue

                yield packet

            except Exception:
                self.connection.disconnect()
                raise

    def receive_packet(self):
        packet = self.connection.receive_packet()

        if packet.type == ServerPacketTypes.EXCEPTION:
            raise packet.exception

        elif packet.type == ServerPacketTypes.PROGRESS:
            return packet

        elif packet.type == ServerPacketTypes.END_OF_STREAM:
            return False

        elif packet.type == ServerPacketTypes.DATA:
            return packet

        elif packet.type == ServerPacketTypes.TOTALS:
            return packet

        elif packet.type == ServerPacketTypes.EXTREMES:
            return packet

        else:
            return True

    def execute(self, query, params=None, with_column_types=False,
                external_tables=None, query_id=None, settings=None,
                types_check=False, columnar=False):

        query_settings = self.settings.copy()
        query_settings.update(settings or {})

        self.connection.context.settings = query_settings

        self.connection.force_connect()

        try:
            # INSERT queries can use list or tuple of list/tuples/dicts.
            # For SELECT parameters can be passed in only in dict right now.
            is_insert = isinstance(params, (list, tuple))
            if is_insert:
                return self.process_insert_query(
                    query, params, external_tables=external_tables,
                    query_id=query_id, settings=query_settings,
                    types_check=types_check
                )
            else:
                return self.process_ordinary_query(
                    query, params=params, with_column_types=with_column_types,
                    external_tables=external_tables,
                    query_id=query_id, settings=query_settings,
                    types_check=types_check, columnar=columnar
                )

        except Exception:
            self.connection.disconnect()
            raise

    def execute_with_progress(
            self, query, params=None, with_column_types=False,
            external_tables=None, query_id=None, settings=None,
            types_check=False):

        query_settings = self.settings.copy()
        query_settings.update(settings or {})

        self.connection.context.settings = query_settings

        self.connection.force_connect()

        try:
            return self.process_ordinary_query_with_progress(
                query, params=params, with_column_types=with_column_types,
                external_tables=external_tables,
                query_id=query_id, settings=query_settings,
                types_check=types_check
            )

        except Exception:
            self.connection.disconnect()
            raise

    def process_ordinary_query_with_progress(
            self, query, params=None, with_column_types=False,
            external_tables=None, query_id=None, settings=None,
            types_check=False, columnar=False):

        if params is not None:
            query = self.substitute_params(query, params)

        self.connection.send_query(query, query_id=query_id, settings=settings)
        self.connection.send_external_tables(external_tables,
                                             types_check=types_check)
        return self.receive_result(with_column_types=with_column_types,
                                   progress=True, columnar=columnar)

    def process_ordinary_query(
            self, query, params=None, with_column_types=False,
            external_tables=None, query_id=None, settings=None,
            types_check=False, columnar=False):

        if params is not None:
            query = self.substitute_params(query, params)

        self.connection.send_query(query, query_id=query_id, settings=settings)
        self.connection.send_external_tables(external_tables,
                                             types_check=types_check)
        return self.receive_result(with_column_types=with_column_types,
                                   columnar=columnar)

    def process_insert_query(self, query_without_data, data,
                             external_tables=None, query_id=None,
                             settings=None, types_check=False):
        self.connection.send_query(query_without_data, query_id=query_id,
                                   settings=settings)
        self.connection.send_external_tables(external_tables,
                                             types_check=types_check)

        sample_block = self.receive_sample_block()
        if sample_block:
            self.send_data(sample_block, data, types_check=types_check)
            packet = self.connection.receive_packet()
            if packet.exception:
                raise packet.exception

    def receive_sample_block(self):
        packet = self.connection.receive_packet()

        if packet.type == ServerPacketTypes.DATA:
            return packet.block

        elif packet.type == ServerPacketTypes.EXCEPTION:
            raise packet.exception

        else:
            message = self.connection.unexpected_packet_message('Data',
                                                                packet.type)
            raise errors.UnexpectedPacketFromServerError(message)

    def send_data(self, sample_block, data, types_check=False):
        client_settings = self.connection.context.client_settings
        for chunk in chunks(data, client_settings['insert_block_size']):
            block = Block(sample_block.columns_with_types, chunk,
                          types_check=types_check)
            self.connection.send_data(block)

        # Empty block means end of data.
        self.connection.send_data(Block())

    def cancel(self, with_column_types=False):
        # TODO: Add warning if already cancelled.
        self.connection.send_cancel()
        # Client must still read until END_OF_STREAM packet.
        return self.receive_result(with_column_types=with_column_types)

    def substitute_params(self, query, params):
        if not isinstance(params, dict):
            raise ValueError('Parameters are expected in dict form')

        escaped = escape_params(params)
        return query % escaped
