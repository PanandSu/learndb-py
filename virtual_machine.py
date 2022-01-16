# from __future__ import annotations
import logging
from typing import Union

from constants import CATALOG
from cursor import Cursor
from btree import Tree, TreeInsertResult, TreeDeleteResult
from statemanager import StateManager
from schema import create_record, create_catalog_record, generate_schema, join_records, Record, schema_to_ddl
from serde import serialize_record, deserialize_cell
from dataexchange import Response

from lang_parser.visitor import Visitor
from lang_parser.tokens import TokenType
from lang_parser.symbols import (
    Token,
    Symbol,
    Program,
    CreateStmnt,
    SelectExpr,
    DropStmnt,
    InsertStmnt,
    DeleteStmnt,
    UpdateStmnt,
    TruncateStmnt,
    Joining,
    JoinType,
    OnClause,
    AliasableSource,
    WhereClause
)
from lang_parser.sqlhandler import SqlFrontEnd


class ExecutionException(Exception):
    """Some error while VM was running
    """
    pass


class RecordSetIter:
    """
    This is an iterator over a RecordSet
    """
    def __init__(self, record_set: 'RecordSet'):
        self.record_set = record_set
        self.recordidx = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.recordidx >= len(self.record_set):
            raise StopIteration
        value = self.record_set[self.recordidx]
        self.recordidx += 1
        return value


class SerializedRecordIter:
    """
    This is an iterator of serialized records.
    This should encapsulate the entire logic around
    cursor advancing and yielding records.

    The reason for creating and naming this, is so that
    there can be uniform API around DeserializedRecordIter,
    which would contain in-memory record objects, e.g. from a join
    op.
    """
    def __init__(self, cursor, schema):
        self.cursor = cursor
        self.schema = schema

    def __iter__(self):
        return self

    def __next__(self):
        if self.cursor.end_of_table:
            raise StopIteration

        cell = self.cursor.get_cell()
        resp = deserialize_cell(cell, self.schema)
        assert resp.success
        record = resp.body
        self.cursor.advance()
        return record


class RecordSet:
    """
    A iterable set of records.
    """
    def __init__(self):
        self.records = []

    def __len__(self):
        return len(self.records)

    def __getitem__(self, idx: int):
        return self.records[idx]

    def get_record(self, idx: int):
        return self.records[idx]

    def append(self, record):
        self.records.append(record)


class VirtualMachine(Visitor):
    """
    Execute prepared statements corresponding to some sql statements, on some
    state. The state is encoded as a catalog (which maps to the table
    containing information of all objects (tables + indices).

    The VM implements different top-level methods corresponding
    to different sql statement type, e.g. create, update, delete, truncate statements.
    Each of these ops will typically have some of the following phases:
        - name resolution
        - optimize
        - execute op

    """
    def __init__(self, state_manager: StateManager, output_pipe: 'Pipe'):
        self.state_manager = state_manager
        self.output_pipe = output_pipe
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

    @staticmethod
    def resolve_table_name(symbol: Symbol) -> str:
        """
        attempt to resolve table name by inspecting argument symbol
        :param symbol:
        :return:
        """

        if isinstance(symbol, SelectExpr):
            # TODO: is this clause needed?
            return symbol.from_location.literal.lower()
        elif isinstance(symbol, (DeleteStmnt, InsertStmnt, DropStmnt, TruncateStmnt, UpdateStmnt)):
            return symbol.table_name.literal.lower()
        return None

    def run(self, program):
        """
        run the virtual machine with program on state
        :param program:
        :return:
        """
        result = []
        for stmt in program.statements:
            try:
                result.append(self.execute(stmt))
            except Exception:
                logging.error(f"ERROR: virtual machine failed on: [{stmt}]")
                raise

    def execute(self, stmnt: 'Symbol'):
        """
        execute statement
        :param stmnt:
        :return:
        """
        return stmnt.accept(self)

    # section : top-level handlers

    def visit_program(self, program: Program) -> Response:
        for stmt in program.statements:
            # note sure how to collect result
            self.execute(stmt)
        return Response(True)

    def visit_create_stmnt(self, stmnt: CreateStmnt) -> Response:
        """

        :param stmnt:
        :return:
        """
        # print(f"In vm: creating table [name={stmnt.table_name}, cols={stmnt.column_def_list}]")

        # 1. generate schema from stmnt
        response = generate_schema(stmnt)
        if response.success is False:
            # schema generation failed
            return Response(False, error_message=f'schema generation failed due to [{response.error_message}]')

        # if generation succeeded, then schema is valid
        table_schema = response.body
        table_name = table_schema.name
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

    def get_record_iter(self, source: AliasableSource):
        """
        This should return an iterator over the record.
        NOTE: The source can be either a table or joined/materialized
        recordset. This should handle both

        :return: TODO: this should return an iterator, i.e. an object that has __next__
        """

        # 1. handle source is a single unmaterialized/serialized table
        if is_single_table:
            return SerializedRecordIter(cursor, schema)
        else:
            # 2. handle table is an existing rowset
            # TODO: this would require that the generated rowset is accessible here
            return RecordSetIter(record_set)

    def materialize_source(self, source: AliasableSource) -> RecordSet:
        """
        This should handle the materialization of source,
        and return a set of rows

        For now, I need to handle:
            - single tables
            - joined tables
        eventually, will also have to handle nested select expr,
        both correlated and uncorrelated.

        For single and joined tables, this will create cursor
        over each of the source tables and loop/iterate and add
        the record

        For tables this should be just a list of deser objects

        Not sure if where condition should be handled here.
        My leaning is that, it should- that will simplify the
        logic around correlated subqueries.

        :return:
        """
        rset = RecordSet()
        # 1. handle single source
        if isinstance(source.source_name, Token):
            table_name = source.source_name.literal
            if table_name != CATALOG and not self.state_manager.table_exists(table_name):
                raise ExecutionException(f"table [{table_name}] does not exist")

            # todo: remove special handling of catalog here
            # perhaps statemanager is aware of catalog
            if table_name == CATALOG:
                # system table/objects
                tree = self.state_manager.get_catalog_tree()
                schema = self.state_manager.get_catalog_schema()
            else:
                # user tables/objects
                tree = self.state_manager.get_tree(table_name)
                schema = self.state_manager.get_schema(table_name)

            # iterate cursor and add to result set
            cursor = Cursor(self.state_manager.get_pager(), tree)
            while cursor.end_of_table is False:
                cell = cursor.get_cell()
                resp = deserialize_cell(cell, schema)
                assert resp.success
                record = resp.body
                rset.append(record)
                cursor.advance()

            return rset

        assert isinstance(source.source_name, Joining)
        # 2. handle joined sources
        stack = []
        # materialize the most nested element in the joining first
        while isinstance(source.source_name, Joining):
            # check if joining has a nested joining
            child = source.source_name
            # recurse
            stack.append(child)
            # the parser nests joins s.t. left source is the child join
            source = child.left_source

        # 3. perform join by walking up the recursive stack
        is_innermost_join = True
        while stack:
            joining = stack.pop()
            # get left source
            # NOTE: in most nested joining, left source will be a table
            # however, after that, it will be a previously joined recordSet
            if is_innermost_join:
                left_src = joining.left_source
                left_record_iter = self.get_record_iter(left_src)
                assert left_src.alias_name and left_src.alias_name.literal is not None, "alias is required"
                left_alias = left_src.alias_name.literal
            else:
                left_record_iter = RecordSetIter(rset)
                left_alias = None
            # get right source
            right_src = joining.right_source
            assert right_src.alias_name and right_src.alias_name.literal is not None, "alias is required"

            # rset created for next join
            next_rset = RecordSet()
            # `get_record_iter` transparently handles both cursor and
            # pre-joined sources
            for left_record in left_record_iter:
                for right_record in self.get_record_iter(right_src):
                    # add joined record if cross join or on condition is true

                    # check if the on clause evaluates to true
                    # TODO: the below call may need to be passed the table aliases
                    evaluation = self.evaluate_on_clause(joining.on_clause, left_record, right_record)
                    if evaluation:
                        # on-cond evaluated true; add joined record
                        # to result set
                        joined_record = join_records(left_record, right_record,
                                                     left_alias=left_alias, right_alias=right_src.alias_name.literal)
                        next_rset.append(joined_record)
                    elif joining.join_type == JoinType.LeftOuter:
                        # for left- and right outer join, if evaluation fails, there should be at least one record
                        # with other side column set to null
                        raise NotImplementedError
                    elif joining.join_type == JoinType.RightOuter:
                        raise NotImplementedError
                    elif joining.join_type == JoinType.FullOuter:
                        raise NotImplementedError

            # prepare for next join op
            # inner_most join is only True one
            is_innermost_join = False
            rset = next_rset

        return rset

    def visit_select_expr(self, expr: SelectExpr, parent_context = None):
        """
        Handle select expr.

        NOTE: this will need to handle 2 things
            - other clauses, i.e.. join, group by, order by, having
            - nested select expr
                -- this will require rethinking how select is exec'ed and results outputted

        :param expr:
        :param parent_context: unused- would be needed for correlated queries
        :return:
        """
        self.output_pipe.reset()

        record_set = self.materialize_source(expr.from_location)
        record_set_iter = RecordSetIter(record_set)

        # iterate record set over all records
        for record in record_set_iter:
            # evaluate where condition and filter non-matching rows
            if self.evaluate_where_clause(expr.where_clause, record) is True:
                # write to output if matches condition
                self.output_pipe.write(record)

    def visit_insert_stmnt(self, stmnt: InsertStmnt):
        """
        Handle insert stmnt

        :param stmnt:
        :return:
        """
        table_name = self.resolve_table_name(stmnt)

        # get schema
        schema = self.state_manager.get_schema(table_name)

        # extract values from stmnt and construct record
        # extract literal from tokens
        column_names = [col_token.literal for col_token in stmnt.column_name_list]
        # cast value literals into the correct type
        # vm is responsible for glue between parser and execution
        value_list = [value_token.value.literal for value_token in stmnt.value_list]
        resp = create_record(column_names, value_list, schema)
        assert resp.success, f"create record failed due to {resp.error_message}"
        record = resp.body
        # get table's tree
        tree = self.state_manager.get_tree(table_name)

        resp = serialize_record(record)
        assert resp.success, f"serialize record failed due to {resp.error_message}"

        cell = resp.body
        resp = tree.insert(cell)
        assert resp == TreeInsertResult.Success, f"Insert op failed with status: {resp}"

    def visit_delete_stmnt(self, stmnt: DeleteStmnt):
        """
        Handle delete stmnt.

        NOTE: the delete condition can cover multiple rows
        for now where cond is restricted to equality condition

        :param stmnt:
        :return:
        """
        # identifier are case sensitive, so don't convert
        table_name = self.resolve_table_name(stmnt)
        # check table is not catalog
        assert table_name != CATALOG, "cannot delete table from catalog; use drop table"

        # get tree and schema
        tree = self.state_manager.get_tree(table_name)
        schema = self.state_manager.get_schema(table_name)

        # scan table and determine which keys to delete, based on where condition
        cursor = Cursor(self.state_manager.get_pager(), tree)
        # keys to delete based on where condition
        del_keys = []

        # get primary key column name
        primary_key_col = schema.get_primary_key_column()

        # iterate cursor
        while cursor.end_of_table is False:
            cell = cursor.get_cell()
            # NOTE: if the condition is only on the primary key,
            # an optimization could be to only deserialize the key and not the entire record
            resp = deserialize_cell(cell, schema)
            assert resp.success
            record = resp.body

            if self.evaluate_where_clause(stmnt.where_clause, record):
                del_key = record.get(primary_key_col)
                del_keys.append(del_key)

            cursor.advance()
        
        # delete matching keys
        for del_key in del_keys:
            resp = tree.delete(del_key)
            assert resp == TreeDeleteResult.Success, f"delete failed for key {del_key}"

    def visit_drop_stmnt(self, stmnt: DropStmnt):
        """
        handle drop statement to drop a table from catalog
        :param stmnt:
        :return:
        """
        table_name = self.resolve_table_name(stmnt)

        catalog_tree = self.state_manager.get_catalog_tree()
        catalog_schema = self.state_manager.get_catalog_schema()
        # scan table and determine which keys to delete, based on where condition
        cursor = Cursor(self.state_manager.get_pager(), catalog_tree)
        # keys to delete based on where condition
        # there should only be a single key, i.e. the table with name
        del_keys = []

        raise NotImplementedError

    def visit_truncate_stmnt(self, stmnt: TruncateStmnt):
        pass

    def visit_update_stmnt(self, stmnt: UpdateStmnt):
        pass

    # section : sub-statement handlers

    def evaluate_on_clause(self, on_clause: OnClause, left_record, right_record) -> bool:
        """
        evaluate on condition

        :return:
        """
        if on_clause is None:
            # no-condition; all results pass
            return True

        # result of or over all or-clauses
        or_result = False
        # NOTE: `on_clause.or_clause` is a list of and'ed predicates
        # at least one and clause must be true for condition to be true
        for and_clause in on_clause.or_clause:
            # result of and over and clauses within or-clause
            and_result = True
            for predicate in and_clause.predicates:
                # decompose predicate
                # the predicate contains 2 operands: left and right;
                # the predicate could refer to one or more of left_, right_ record, or value
                left_token = predicate.first.value
                right_token = predicate.second.value

                # TODO: revolve value of left_token in is column_ref ; likewise for right_token
                # below code needs to be updated

                if left_token.token_type == TokenType.IDENTIFIER:
                    column = left_token.literal
                    cond_value = right_token.literal
                else:
                    cond_value = left_token.literal
                    column = right_token.literal

                # determine the record's value for given column
                record_value = record.get(column)
                # evaluate predicate
                if predicate.op.token_type == TokenType.EQUAL:
                    pred_val = record_value == cond_value
                elif predicate.op.token_type == TokenType.NOT_EQUAL:
                    pred_val = record_value != cond_value
                elif predicate.op.token_type == TokenType.LESS_EQUAL:
                    pred_val = record_value <= cond_value
                elif predicate.op.token_type == TokenType.LESS:
                    pred_val = record_value < cond_value
                elif predicate.op.token_type == TokenType.GREATER_EQUAL:
                    pred_val = record_value >= cond_value
                else:
                    assert predicate.op.token_type == TokenType.GREATER
                    pred_val = record_value > cond_value

                and_result = and_result and pred_val

            or_result = or_result or and_result
            if or_result:
                # condition is true, eagerly exit
                return True
        return False

    def evaluate_where_clause(self, where_clause: WhereClause, record: Record) -> bool:
        """
        evaluate condition

        on (join) could be implemented similarly
        :param where_clause:
        :param record:
        :return:
        """
        if where_clause is None:
            # no-condition; all results pass
            return True

        # result of or over all or-clauses
        or_result = False
        # NOTE: `where_clause.or_clause` is a list of and'ed predicates
        # at least one and clause must be true for condition to be true
        for and_clause in where_clause.or_clause:
            # result of and over and clauses within or-clause
            and_result = True
            for predicate in and_clause.predicates:
                # decompose predicate
                # determine which operand contains value and which contains column name
                left_token = predicate.first.value
                right_token = predicate.second.value
                if left_token.token_type == TokenType.IDENTIFIER:
                    column = left_token.literal
                    cond_value = right_token.literal
                else:
                    cond_value = left_token.literal
                    column = right_token.literal

                # determine the record's value for given column
                record_value = record.get(column)
                # evaluate predicate
                if predicate.op.token_type == TokenType.EQUAL:
                    pred_val = record_value == cond_value
                elif predicate.op.token_type == TokenType.NOT_EQUAL:
                    pred_val = record_value != cond_value
                elif predicate.op.token_type == TokenType.LESS_EQUAL:
                    pred_val = record_value <= cond_value
                elif predicate.op.token_type == TokenType.LESS:
                    pred_val = record_value < cond_value
                elif predicate.op.token_type == TokenType.GREATER_EQUAL:
                    pred_val = record_value >= cond_value
                else:
                    assert predicate.op.token_type == TokenType.GREATER
                    pred_val = record_value > cond_value

                and_result = and_result and pred_val

            or_result = or_result or and_result
            if or_result:
                # condition is true, eagerly exit
                return True
        return False
