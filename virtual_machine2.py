"""
Rewrite of virtual machine class.
This focuses on select execution- later merge
the rest of the logic from the old VM in here

"""
import logging
import random
import string

from typing import List

from btree import Tree, TreeInsertResult, TreeDeleteResult
from cursor import Cursor
from dataexchange import Response
from schema import generate_schema, schema_to_ddl
from serde import serialize_record, deserialize_cell

from lark import Token
from lang_parser.visitor import Visitor
from lang_parser.symbols3 import (
    Symbol,
    Program,
    CreateStmnt,
    FromClause,
    SingleSource,
    UnconditionedJoin,
    JoinType,
    Joining,
    ConditionedJoin,
    Comparison,
    ComparisonOp,
    WhereClause,
    TableName
)

from lang_parser.sqlhandler import SqlFrontEnd

from record_utils import (
    Record,
    create_catalog_record,
    join_records,
    JoinedRecord,
    create_record,
)


logger = logging.getLogger(__name__)


class RecordSet:
    pass


class GroupedRecordSet(RecordSet):
    pass


class NullRecordSet(RecordSet):
    pass


class VirtualMachine(Visitor):
    """
    New virtual machine.
    Compared to the previous impl; this will add or improve:
        - api for creating, manipulating, and combining RecordSets

    There are 2 flavors of RecordSets: RecordSet, GroupedRecordSet, and possibly a EmptyRecordSet
    The RS api would consist of:
        init_recordset(name)
        append_recordset(name, record)
        drop_recordset(name)

        init_groupedrecordset(name, group_vec)
        append_groupedrecordset(name, record)
        drop_groupedrecordset(name)

    These will likely be backed by simple lists, and dicts of lists.
    The main reason for doing it this way; instead of making rich
    RecordSet types, is because that would require logic for interpreting
    symbols into RecordSet; and I want that logic to not be replicated outside vm.

    """
    def __init__(self, state_manager, output_pipe):
        self.state_manager = state_manager
        self.output_pipe = output_pipe
        # NOTE: some state is managed by the state_manager, and rest via
        # vm's instance variable. Ultimately, there should be a cleaner,
        # unified management of state
        self.rsets = {}
        self.grouprsets = {}
        self.init_catalog()

    def init_catalog(self):
        """
        Initialize the catalog
        read the catalog, materialize table metadata and register with the statemanager
        :return:
        """
        # get/register all tables' metadata from catalog
        catalog_tree = self.state_manager.get_catalog_tree()
        catalog_schema = self.state_manager.get_catalog_schema()
        pager = self.state_manager.get_pager()
        cursor = Cursor(pager, catalog_tree)

        # need parser to parse schema definition
        parser = SqlFrontEnd()

        # iterate over table entries
        while cursor.end_of_table is False:
            cell = cursor.get_cell()
            resp = deserialize_cell(cell, catalog_schema)
            assert resp.success, "deserialize failed while bootstrapping catalog"
            table_record = resp.body

            # get schema by parsing sql_text
            sql_text = table_record.get("sql_text")
            logging.info(f"bootstrapping schema from [{sql_text}]")
            parser.parse(sql_text)
            assert parser.is_success(), "catalog sql parse failed"
            program = parser.get_parsed()
            assert len(program.statements) == 1
            stmnt = program.statements[0]
            assert isinstance(stmnt, CreateStmnt)
            resp = generate_schema(stmnt)
            assert resp.success, "schema generation failed"
            table_schema = resp.body

            # get tree
            # should vm be responsible for this
            tree = Tree(self.state_manager.get_pager(), table_record.get("root_pagenum"))

            # register schema
            self.state_manager.register_schema(table_record.get("name"), table_schema)
            self.state_manager.register_tree(table_record.get("name"), tree)

            cursor.advance()

    def run(self, program, stop_on_err=False) -> List:
        """
        run the virtual machine with program on state
        :param program:
        :param stop_on_err: stop program execution on first error
        :return:
        """
        result = []
        for stmt in program.statements:
            try:
                resp = self.execute(stmt)
                # todo: why isn't resp always Response
                if isinstance(resp, Response) and not resp.success:
                    logging.warning(f"Statement [{stmt}] failed with {resp}")
                result.append(resp)
                if not resp and stop_on_err:
                    return result
            except Exception as e:
                logging.error(f"ERROR: virtual machine errored on: [{stmt}] with [{e}|]")
                # ultimately this should not throw
                raise
        return result

    def execute(self, stmnt: Symbol):
        """
        execute statement
        :param stmnt:
        :return:
        """
        return stmnt.accept(self)

    # section : top-level statement handlers

    def visit_program(self, program: Program) -> Response:
        for stmt in program.statements:
            # note sure how to collect result
            self.execute(stmt)
        return Response(True)

    def visit_create_stmnt(self, stmnt: CreateStmnt) -> Response:
        """
        Handle create stmnt
        generate, validate, and persisted schema.
        """
        # 1.attempt to generate schema from create_stmnt
        response = generate_schema(stmnt)
        if not response.success:
            # schema generation failed
            return Response(False, error_message=f'schema generation failed due to [{response.error_message}]')
        table_schema = response.body
        table_name = table_schema.name.table_name
        assert isinstance(table_name, str), "table_name is not string"

        # 2. check whether table name is unique
        assert self.state_manager.table_exists(table_name) is False, f"table {table_name} exists"

        # 3. allocate tree for new table
        page_num = self.state_manager.allocate_tree()

        # 4. construct record for table
        # NOTE: for now using page_num as unique int key
        pkey = page_num
        sql_text = schema_to_ddl(table_schema)
        # logging.info(f'visit_create_stmnt: generated DDL: {sql_text}')
        catalog_schema = self.state_manager.get_catalog_schema()
        response = create_catalog_record(pkey, table_name, page_num, sql_text, catalog_schema)
        if not response.success:
            return Response(False, error_message=f'Failure due to {response.error_message}')

        # 5. serialize table record, i.e. record in catalog table for new user table
        table_record = response.body
        response = serialize_record(table_record)
        if not response.success:
            return Response(False, error_message=f'Serialization failed: [{response.error_message}]')

        # 6. insert entry into catalog tree
        cell = response.body
        catalog_tree = self.state_manager.get_catalog_tree()
        catalog_tree.insert(cell)

        # 7. register schema
        self.state_manager.register_schema(table_name, table_schema)
        # 8. register tree
        tree = Tree(self.state_manager.get_pager(), table_record.get("root_pagenum"))
        self.state_manager.register_tree(table_name, tree)

    def visit_select_stmnt(self, stmnt) -> Response:
        """
        handle select stmnt
        most clauses are optional
        """
        # 1. setup
        self.output_pipe.reset()

        # 2. check and handle from clause
        from_clause = stmnt.from_clause
        if from_clause:
            # materialize source in from clause
            resp = self.materialize(stmnt.from_clause.source.source)
            if not resp.success:
                return Response(False, error_message=f"[from_clause] source materialization failed due to {resp.error_message}")
            rsname = resp.body

            # 3. apply filter on source - where clause
            if from_clause.where_clause:
                resp = self.filter_recordset(from_clause.where_clause, rsname)
                if not resp.success:
                    return Response(False, error_message=f"[where_clause] filtering failed due to {resp.error_message}")
                # filtering produces a new resultset
                rsname = resp.body

            if from_clause.group_by_clause:
                pass
            if from_clause.having_clause:
                pass
            if from_clause.order_by_clause:
                pass
            if from_clause.limit_clause:
                pass

            for record in self.recordset_iter(rsname):
                # todo: create records with only selected columns
                self.output_pipe.write(record)

        # output pipe for sanity
        # for msg in self.output_pipe.store:
        #    logger.info(msg)

    def visit_insert_stmnt(self, stmnt) -> Response:
        """
        handle insert stmnt
        """
        table_name = stmnt.table_name.table_name
        if not self.state_manager.has_schema(table_name):
            # check if table exists
            return Response(False, error_message=f"Table [{table_name}] does not exist")

        # get schema
        schema = self.state_manager.get_schema(table_name)
        resp = create_record(stmnt.column_name_list, stmnt.value_list, schema)
        if not resp.success:
            return Response(False, error_message=f"Insert record failed due to [{resp.error_message}]")

        record = resp.body
        # get table's tree
        tree = self.state_manager.get_tree(table_name)

        resp = serialize_record(record)
        assert resp.success, f"serialize record failed due to {resp.error_message}"

        cell = resp.body
        resp = tree.insert(cell)
        assert resp == TreeInsertResult.Success, f"Insert op failed with status: {resp}"

    def visit_delete_stmnt(self, stmnt) -> Response:
        """
        handle delete stmnt
        """

        # 1. iterate over source dataset
        # materializing the entire recordset is expensive, but cleaner/easier/faster to implement
        resp = self.materialize(stmnt.table_name)
        assert resp.success
        rsname = resp.body

        if stmnt.where_condition:
            resp = self.filter_recordset(stmnt.where_condition, rsname)
            assert resp.success
            rsname = resp.body

        # 2. create list of keys to delete
        del_keys = []
        for record in self.recordset_iter(rsname):
            del_keys.append(record.get_primary_key())

        # 3. delete the keys
        table_name = stmnt.table_name.table_name
        tree = self.get_tree(table_name)
        for del_key in del_keys:
            resp = tree.delete(del_key)
            if resp != TreeDeleteResult.Success:
                logging.warning(f"delete failed for key {del_key}")
                return Response(False, resp)
        # return list of deleted keys
        return Response(True, body=del_keys)


    # section : statement helpers
    # general principles:
    # 1) helpers should be able to handle null types

    def get_schema(self, table_name):
        if table_name.table_name.lower() == "catalog":
            return self.state_manager.get_catalog_schema()
        else:
            return self.state_manager.get_schema(table_name.table_name)

    def get_tree(self, table_name):
        if table_name.table_name.lower() == "catalog":
            return self.state_manager.get_catalog_tree()
        else:
            return self.state_manager.get_tree(table_name.table_name)

    def materialize(self, source) -> Response:
        """

        """
        if isinstance(source, SingleSource):
            # NOTE: single source means a single physical table
            return self.materialize_single_source(source)

        elif isinstance(source, Joining):
            return self.materialize_joining(source)

        elif isinstance(source, TableName):
            return self.materialized_source_from_name(source.table_name)

        else:
            raise ValueError(f"Unknown materialization source type {source}")
        # case nestedSelect

    def materialize_single_source(self, source: SingleSource) -> Response:
        """
        Materialize single source and return
        """
        assert isinstance(source, SingleSource), f"Unexpected {source}"

        # does table_names need to be resolved?
        return self.materialized_source_from_name(source.table_name)

    def materialized_source_from_name(self, table_name: str) -> Response:
        resp = self.init_recordset()
        assert resp.success
        rsname = resp.body

        # get schema for table, and cursor on tree corresponding to table
        schema = self.get_schema(table_name)
        tree = self.get_tree(table_name)

        cursor = Cursor(self.state_manager.get_pager(), tree)
        # iterate over entire table
        while cursor.end_of_table is False:
            cell = cursor.get_cell()
            resp = deserialize_cell(cell, schema)
            assert resp.success
            record = resp.body
            self.append_recordset(rsname, record)
            # advance cursor
            cursor.advance()
        return Response(True, body=rsname)

    def materialize_joining(self, source: Joining) -> Response:
        """
        Materialize a joining.
        After a pairwise joining of recordsets
        """
        # parser places first table in a series of joins in the most nested
        # join; recursively traverse the join object(s) and construct a ordered list of
        # tables to materialize
        stack = [source]
        ptr = source
        while True:
            stack.append(ptr.left_source)
            if isinstance(ptr.left_source, Joining):
                # recurse down
                ptr = ptr.left_source
            else:
                assert isinstance(ptr.left_source, SingleSource)
                # end of join nesting
                break

        # now materialize joins
        # starting from stack top, each materialization is the left_source
        # in the nest iteration of joining
        first = stack.pop()
        resp = self.materialize(first)
        assert resp.success
        rsname = resp.body
        left_source_name = first.table_alias or first.table_name.table_name
        while stack:
            # join next source with existing rset
            next_join = stack.pop()
            assert isinstance(next_join, Joining)
            # NOTE: currently, materialize and join are separate steps
            # these could be combined; but for now keep them separate
            # to keep it simple

            right_source = next_join.right_source
            # name within local scope, i.e. tablename or alias
            right_source_name = right_source.table_alias or right_source.table_name.table_name
            resp = self.materialize_single_source(right_source)
            assert resp.success
            next_rsname = resp.body

            # join next_join with rsname
            # NOTE: in the first joining, i.e. when 2 simple sources are joined, I need to pass
            # the table_name for left and right sources. This creates a joined_recordsets, containing
            # JoinedRecords. JoinedRecords, unlike (simple) Records, contain the table_name, where the
            # record came from. Thus, in subsequent joinings, i.e. the previously joined with a simple
            # source (containing Records), I only need to pass the alias for the right source, since
            # the JoinedRecord already contains the table name info

            # NOTE: this API could be made more consistent by always working with JoinedRecord, even
            # for simple joins; also no need to refactor this now; once I test more complex cases
            # the API will become clearer

            resp = self.join_recordset(next_join, rsname, next_rsname, left_source_name, right_source_name)
            left_source_name = None
            assert resp.success
            rsname = resp.body
            #for record in self.recordset_iter(rsname):
            #    logging.info(f"Sanity: {record}")
            #logging.info(f"Sanity end")

        return Response(True, body=rsname)

    def check_resolve_name(self, operand, record) -> Response:
        """
        Check if the `operand` is logical name, if so
        attempt to resolve it from record
        """
        if isinstance(operand, Token):
            if operand.type == "IDENTIFIER":
                # check if we can resolve this
                assert isinstance(record, Record)  # this may not needed, but this is what this logic is assuming
                if operand in record.values:
                    return Response(True, body=record.values[operand])
            elif operand.type == "SCOPED_IDENTIFIER":
                # attempt resolve scoped identifier
                assert isinstance(record, JoinedRecord)
                value = record.get(operand)
                return Response(True, body=value)
            return Response(False, f"Unable to resolve {operand.type}")
        else:
            # not name, return as is
            return Response(True, body=operand)

    def filter_recordset(self, where_clause: WhereClause, source_rsname: str) -> Response:
        """
        Apply where_clause on source_rsname and return filtered resultset
        """
        assert isinstance(where_clause, WhereClause)

        resp = self.init_recordset()
        assert resp.success
        # generate new result set
        rsname = resp.body

        for record in self.recordset_iter(source_rsname):
            or_result = False
            # or the result of each and_clause
            for and_clause in where_clause.condition.and_clauses:
                and_result = True
                for predicate in and_clause.predicates:
                    assert isinstance(predicate, Comparison), f"Expected Comparison received {predicate}"
                    # resolve any logical names
                    resp = self.check_resolve_name(predicate.left_op, record)
                    assert resp.success
                    left_value = resp.body
                    resp = self.check_resolve_name(predicate.right_op, record)
                    assert resp.success
                    right_value = resp.body
                    # value of evaluated predicate
                    pred_value = False
                    if predicate.operator == ComparisonOp.Greater:
                        pred_value = left_value > right_value
                    elif predicate.operator == ComparisonOp.Less:
                        pred_value = left_value < right_value
                    elif predicate.operator == ComparisonOp.GreaterEqual:
                        pred_value = left_value >= right_value
                    elif predicate.operator == ComparisonOp.LessEqual:
                        pred_value = left_value <= right_value
                    elif predicate.operator == ComparisonOp.Equal:
                        pred_value = left_value == right_value
                    else:
                        assert predicate.operator == ComparisonOp.NotEqual
                        pred_value = left_value != right_value

                    # optimization note: once an and_result is False, stop loop
                    and_result = and_result and pred_value

                or_result = or_result or and_result

            if or_result:
                self.append_recordset(rsname, record)

        return Response(True, body=rsname)

    def join_recordset(self, join_clause, left_rsname: str, right_rsname: str, left_sname: str, right_sname: str) -> Response:
        """
        join record based on record type and return joined recordset.

        NOTE: this handles 2 cases:
            - joins over simple sources (singular_join)
            - joins over 1 simple source, and joined source (multi_join)

        TOOD: the tricky bit here, handling 2 single sources, and subsequently
        a single source, and a joined source

        :param left_rsname: left record set name (could be single or joined recordset)
        :param right_rsname: right record set name (single source)
        :param left_sname: left source name
        :param left_sname: right source name
        """
        resp = self.init_recordset()
        assert resp.success
        rsname = resp.body

        # distinguish between simple and multi-join by checking if left_sname is set
        singular_join = left_sname is not None

        left_iter = self.recordset_iter(left_rsname)
        right_iter = self.recordset_iter(right_rsname)

        # inner join
        if join_clause.join_type == JoinType.Inner:
            for left_rec in left_iter:
                for right_rec in right_iter:

                    if singular_join:
                        record = JoinedRecord.from_simple_records(left_rec, right_rec, left_sname, right_sname)
                    else:
                        record = JoinedRecord.from_joined_and_simple_record(left_rec, right_rec, right_sname)
                    or_result = False
                    # TODO: below was copied from filter_results, and should be factored cleanly
                    for and_clause in join_clause.condition.and_clauses:
                        and_result = True
                        for predicate in and_clause.predicates:
                            assert isinstance(predicate, Comparison), f"Expected Comparison received {predicate}"
                            # the predicate contains a condition, which contains
                            # logical column refs and literal values
                            # 1. resolve all names
                            resp = self.check_resolve_name(predicate.left_op, record)
                            assert resp.success
                            left_value = resp.body
                            resp = self.check_resolve_name(predicate.right_op, record)
                            assert resp.success
                            right_value = resp.body

                            # 2. evaluate predicate
                            # value of evaluated predicate
                            pred_value = False
                            if predicate.operator == ComparisonOp.Greater:
                                pred_value = left_value > right_value
                            elif predicate.operator == ComparisonOp.Less:
                                pred_value = left_value < right_value
                            elif predicate.operator == ComparisonOp.GreaterEqual:
                                pred_value = left_value >= right_value
                            elif predicate.operator == ComparisonOp.LessEqual:
                                pred_value = left_value <= right_value
                            elif predicate.operator == ComparisonOp.Equal:
                                pred_value = left_value == right_value
                            else:
                                assert predicate.operator == ComparisonOp.NotEqual
                                pred_value = left_value != right_value

                            # optimization note: once an and_result is False, stop loop
                            and_result = and_result and pred_value

                        or_result = or_result or and_result

                    if or_result:
                        # join condition matched
                        self.append_recordset(rsname, record)


        # todo: implement other joins

        return Response(True, body=rsname)

    def evaluate_on(self, condition, left_record, right_record):
        # TODO: implement
        # this can be similar to filter_results
        breakpoint()


    # section: record set utilities

    def gen_randkey(self, size=10):
        return "".join(random.choice(string.ascii_letters) for i in range(size))

    def init_recordset(self) -> Response:
        """
        initialize recordset; this requires a unique name
        for each recordset
        """
        name = self.gen_randkey()
        while name in self.rsets:
            # generate while non-unique
            name = self.gen_randkey()
        self.rsets[name] = []
        return Response(True, body=name)

    def append_recordset(self, name: str, record):
        assert name in self.rsets
        self.rsets[name].append(record)

    def drop_recordset(self, name: str):
        del self.rsets[name]

    def recordset_iter(self, name: str):
        """return an iterator over recordset"""
        return iter(self.rsets[name])
