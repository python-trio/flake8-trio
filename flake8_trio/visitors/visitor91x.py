"""Contains Visitor91X.

910 looks for async functions without guaranteed checkpoints (or exceptions), and 911 does
the same except for async iterables - while also requiring that they checkpoint between
each yield.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import libcst as cst
import libcst.matchers as m
from libcst.metadata import PositionProvider

from ..base import Statement
from .flake8triovisitor import Flake8TrioVisitor_cst
from .helpers import (
    disabled_by_default,
    error_class_cst,
    fnmatch_qualified_name_cst,
    func_has_decorator,
    iter_guaranteed_once_cst,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


ARTIFICIAL_STATEMENT = Statement("artificial", -1)


def func_empty_body(node: cst.FunctionDef) -> bool:
    """Check if function body consist of `pass`, `...`, and/or (doc)string literals."""
    empty_statement = m.Pass() | m.Expr(m.Ellipsis() | m.SimpleString())
    return m.matches(
        node.body,
        m.IndentedBlock(
            [m.ZeroOrMore(m.SimpleStatementLine([m.ZeroOrMore(empty_statement)]))]
        ),
    )


@dataclass
class LoopState:
    infinite_loop: bool = False
    body_guaranteed_once: bool = False
    has_break: bool = False

    uncheckpointed_before_continue: set[Statement] = field(default_factory=set)
    uncheckpointed_before_break: set[Statement] = field(default_factory=set)

    artificial_errors: set[cst.Return | cst.Yield] = field(default_factory=set)
    nodes_needing_checkpoints: list[cst.Return | cst.Yield] = field(
        default_factory=list
    )
    possibly_redundant_lowlevel_checkpoints: list[cst.BaseExpression] = field(
        default_factory=list
    )

    def copy(self):
        return LoopState(
            infinite_loop=self.infinite_loop,
            body_guaranteed_once=self.body_guaranteed_once,
            has_break=self.has_break,
            uncheckpointed_before_continue=self.uncheckpointed_before_continue.copy(),
            uncheckpointed_before_break=self.uncheckpointed_before_break.copy(),
            artificial_errors=self.artificial_errors.copy(),
            nodes_needing_checkpoints=self.nodes_needing_checkpoints.copy(),
            possibly_redundant_lowlevel_checkpoints=self.possibly_redundant_lowlevel_checkpoints.copy(),
        )


@dataclass
class TryState:
    body_uncheckpointed_statements: set[Statement] = field(default_factory=set)
    try_checkpoint: set[Statement] = field(default_factory=set)
    except_uncheckpointed_statements: set[Statement] = field(default_factory=set)
    added: set[Statement] = field(default_factory=set)

    def copy(self):
        return TryState(
            body_uncheckpointed_statements=self.body_uncheckpointed_statements.copy(),
            try_checkpoint=self.try_checkpoint.copy(),
            except_uncheckpointed_statements=self.except_uncheckpointed_statements.copy(),
            added=self.added.copy(),
        )


def checkpoint_statement(library: str) -> cst.SimpleStatementLine:
    return cst.SimpleStatementLine(
        [cst.Expr(cst.parse_expression(f"await {library}.lowlevel.checkpoint()"))]
    )


class CommonVisitors(cst.CSTTransformer, ABC):
    """Base class for InsertCheckpointsInLoopBody and Visitor91X.

    Contains the transform methods used to actually insert the checkpoints, as well
    as making sure that the library used will get imported. Adding the library import
    is done in Visitor91X.
    """

    def __init__(self):
        super().__init__()
        self.noautofix: bool = False
        self.add_statement: cst.SimpleStatementLine | None = None

        self.explicitly_imported_library: dict[str, bool] = {
            "trio": False,
            "anyio": False,
        }
        self.add_import: set[str] = set()

        self.__booldepth = 0

    @property
    @abstractmethod
    def library(self) -> tuple[str, ...]:
        ...

    @abstractmethod
    def should_autofix(self) -> bool:
        ...

    # instead of trying to exclude yields found in all the weird places from
    # setting self.add_statement, we instead clear it upon each new line.
    # Several of them *could* be handled, e.g. `if ...: yield`, but
    # that's uncommon enough we don't care about it.
    def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine):
        self.add_statement = None

    # insert checkpoint before yield with a flattensentinel, if indicated
    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine | cst.FlattenSentinel[cst.SimpleStatementLine]:
        # possible TODO: generate an error if transforming+visiting is done in a
        # single pass and emit-error-on-transform can be enabled/disabled. The error can't
        # be generated in the yield/return since it doesn't know if it will be autofixed.
        if self.add_statement is None or not self.should_autofix():
            return updated_node
        curr_add_statement = self.add_statement
        self.add_statement = None

        # multiple statements on a single line is not handled
        if len(updated_node.body) > 1:
            return updated_node

        self.ensure_imported_library()
        return cst.FlattenSentinel([curr_add_statement, updated_node])

    def visit_BooleanOperation(self, node: cst.BooleanOperation):
        self.__booldepth += 1
        self.noautofix = True

    def leave_BooleanOperation(
        self, original_node: cst.BooleanOperation, updated_node: cst.BooleanOperation
    ):
        assert self.__booldepth
        self.__booldepth -= 1
        if not self.__booldepth:
            self.noautofix = False
        return updated_node

    def ensure_imported_library(self) -> None:
        """Mark library for import.

        Check that the library we'd use to insert checkpoints
        is imported - if not, mark it to be inserted later.
        """
        assert self.library
        if not self.explicitly_imported_library[self.library[0]]:
            self.add_import.add(self.library[0])


class InsertCheckpointsInLoopBody(CommonVisitors):
    """Insert checkpoints in loop bodies.

    This inserts checkpoints that it was not known on the first pass whether a
    checkpoint would be necessary, i.e. no uncheckpointed statements as we started to
    parse the loop, but then there's uncheckpointed statements on continue or as loop
    body finishes.
    Called from `leave_While` and `leave_For` in Visitor91X.
    """

    def __init__(
        self,
        nodes_needing_checkpoint: Sequence[cst.Yield | cst.Return],
        library: tuple[str, ...],
    ):
        super().__init__()
        self.nodes_needing_checkpoint = nodes_needing_checkpoint
        self.__library = library

    @property
    def library(self) -> tuple[str, ...]:
        return self.__library if self.__library else ("trio",)

    def should_autofix(self) -> bool:
        return True

    def leave_Yield(
        self,
        original_node: cst.Yield,
        updated_node: cst.Yield,
    ) -> cst.Yield:
        # Needs to be passed *original* node, since updated node is a copy
        # which loses identity equality
        if original_node in self.nodes_needing_checkpoint and not self.noautofix:
            self.add_statement = checkpoint_statement(self.library[0])
        return updated_node

    # returns handled same as yield, but ofc needs to ignore types
    leave_Return = leave_Yield  # type: ignore


# class RemoveLowlevelCheckpoints(cst.CSTTransformer):
#    def __init__(self, stmts_to_remove: set[cst.Await]):
#        self.stmts_to_remove = stmts_to_remove
#
#    def leave_Await(self, original_node: cst.Await, updated_node: cst.Await) -> cst.Await:
#        # return original node to preserve identity
#        return original_node
#
#    # for some reason you can't just return RemovalSentinel from Await, so we have to
#    # visit the possible wrappers and modify their bodies instead
#
#    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
#        new_body = [stmt for stmt in updated_node.body.body if stmt not in self.stmts_to_remove]
#        return updated_node.with_changes(body=updated_node.body.with_changes(body=new_body))


@error_class_cst
@disabled_by_default
class Visitor91X(Flake8TrioVisitor_cst, CommonVisitors):
    error_codes = {
        "TRIO910": (
            "{0} from async function with no guaranteed checkpoint or exception "
            "since function definition on line {1.lineno}."
        ),
        "TRIO911": (
            "{0} from async iterable with no guaranteed checkpoint since {1.name} "
            "on line {1.lineno}."
        ),
        "TRIO912": "Redundant checkpoint with no effect on program execution.",
    }

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.has_yield = False
        self.safe_decorator = False
        self.async_function = False
        self.comp_unknown = False

        self.uncheckpointed_statements: set[Statement] = set()
        self.checkpointed_by_lowlevel = False

        # value == False, not redundant (or not determined to be redundant yet)
        # value == True, there were no uncheckpointed statements when we encountered it
        # value = expr/stmt, made redundant by the given expr/stmt
        self.lowlevel_checkpoints: dict[
            cst.Await, cst.BaseStatement | cst.BaseExpression | bool
        ] = {}
        self.lowlevel_checkpoint_updated_nodes: dict[cst.Await, cst.Await] = {}

        self.loop_state = LoopState()
        self.try_state = TryState()

    def should_autofix(self, code: str | None = None) -> bool:
        return super().should_autofix("TRIO911" if self.has_yield else "TRIO910")

    def checkpoint_statement(self) -> cst.SimpleStatementLine:
        return checkpoint_statement(self.library[0])

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        # don't lint functions whose bodies solely consist of pass or ellipsis
        # @overload functions are also guaranteed to be empty
        # we also ignore pytest fixtures
        if func_has_decorator(node, "overload", "fixture") or func_empty_body(node):
            return False  # subnodes can be ignored

        self.save_state(
            node,
            "has_yield",
            "safe_decorator",
            "async_function",
            "uncheckpointed_statements",
            "lowlevel_checkpoints",
            "loop_state",
            "try_state",
            copy=True,
        )
        self.uncheckpointed_statements = set()
        self.has_yield = self.safe_decorator = False
        self.loop_state = LoopState()

        self.async_function = (
            node.asynchronous is not None
            and not fnmatch_qualified_name_cst(
                node.decorators, *self.options.no_checkpoint_warning_decorators
            )
        )
        if not self.async_function:
            # only visit subnodes if there is an async function defined inside
            # this should improve performance on codebases with many sync functions
            return any(m.findall(node, m.FunctionDef(asynchronous=m.Asynchronous())))

        pos = self.get_metadata(PositionProvider, node).start
        self.uncheckpointed_statements = {
            Statement("function definition", pos.line, pos.column)
        }
        return True

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if (
            self.async_function
            # updated_node does not have a position, so we must send original_node
            and self.check_function_exit(original_node)
            and self.should_autofix()
            and isinstance(updated_node.body, cst.IndentedBlock)
        ):
            # insert checkpoint at the end of body
            new_body = list(updated_node.body.body)
            new_body.append(self.checkpoint_statement())
            indentedblock = updated_node.body.with_changes(body=new_body)
            updated_node = updated_node.with_changes(body=indentedblock)

        res: cst.FunctionDef = updated_node
        to_remove: set[cst.Await] = set()
        for expr, value in self.lowlevel_checkpoints.items():
            if value != False:
                self.error(expr, error_code="TRIO912")
                if self.should_autofix():
                    to_remove.add(self.lowlevel_checkpoint_updated_nodes.pop(expr))

        if to_remove:
            new_body = []
            for stmt in updated_node.body.body:
                if not m.matches(
                    stmt,
                    m.SimpleStatementLine(
                        [m.Expr(m.MatchIfTrue(lambda x: x in to_remove))]
                    ),
                ):
                    new_body.append(stmt)  # type: ignore
            assert new_body != updated_node.body.body
            res = updated_node.with_changes(
                body=updated_node.body.with_changes(body=new_body)
            )

        self.restore_state(original_node)
        return res

    # error if function exit/return/yields with uncheckpointed statements
    # returns a bool indicating if any real (i.e. not artificial) errors were raised
    # so caller can insert checkpoint before statement (if yield/return) or at end
    # of body (functiondef)
    def check_function_exit(
        self,
        original_node: cst.FunctionDef | cst.Return | cst.Yield,
    ) -> bool:
        if not self.uncheckpointed_statements:
            return False

        # Artificial statement is injected in visit_While_body to make sure errors
        # are raised on multiple loops, if e.g. the end of a loop is uncheckpointed.
        if ARTIFICIAL_STATEMENT in self.uncheckpointed_statements:
            # function can't end in the middle of a loop body, where artificial
            # statements are injected
            assert not isinstance(original_node, cst.FunctionDef)

            # Add it to artificial errors, so loop logic can later turn it into
            # a real error if needed.
            self.loop_state.artificial_errors.add(original_node)

            # Add this as a node potentially needing checkpoints only if it
            # missing checkpoints solely depends on whether the artificial statement is
            # "real"
            if len(self.uncheckpointed_statements) == 1 and not self.noautofix:
                self.loop_state.nodes_needing_checkpoints.append(original_node)
                return False

        # raise the actual errors
        for statement in self.uncheckpointed_statements:
            if statement == ARTIFICIAL_STATEMENT:
                continue
            self.error_91x(original_node, statement)

        return True

    def leave_Return(
        self, original_node: cst.Return, updated_node: cst.Return
    ) -> cst.Return:
        if not self.async_function:
            return updated_node
        if self.check_function_exit(original_node):
            self.add_statement = self.checkpoint_statement()
        # avoid duplicate error messages
        self.uncheckpointed_statements = set()

        # return original node to avoid problems with identity equality
        assert original_node.deep_equals(updated_node)
        return original_node

    def error_91x(
        self,
        node: cst.Return | cst.FunctionDef | cst.Yield,
        statement: Statement,
    ):
        assert statement != ARTIFICIAL_STATEMENT

        if isinstance(node, cst.FunctionDef):
            msg = "exit"
        else:
            msg = node.__class__.__name__.lower()

        self.error(
            node,
            msg,
            statement,
            error_code="TRIO911" if self.has_yield else "TRIO910",
        )

    def is_lowlevel_checkpoint(self, node: cst.BaseExpression) -> bool:
        # TODO: match against both libraries if both are imported
        return m.matches(
            node,
            m.Call(
                m.Attribute(
                    m.Attribute(m.Name(self.library[0]), m.Name("lowlevel")),
                    m.Name("checkpoint"),
                )
            ),
        )

    def visit_Await(self, node: cst.Await) -> None:
        # do a match against the awaited expr
        # if that is trio.lowlevel.checkpoint, and uncheckpointed statements
        # are empty, raise TRIO912.
        if self.is_lowlevel_checkpoint(node.expression):
            if not self.uncheckpointed_statements:
                self.lowlevel_checkpoints[node] = True
            elif self.uncheckpointed_statements == {ARTIFICIAL_STATEMENT}:
                self.loop_state.possibly_redundant_lowlevel_checkpoints.append(node)
            else:
                self.lowlevel_checkpoints[node] = False
            # if trio.lowlevel.checkpoint and *not* empty, take note of it in a special list.
        elif not self.uncheckpointed_statements:
            for expr, value in self.lowlevel_checkpoints.items():
                if value == False:
                    self.lowlevel_checkpoints[expr] = node

            # if this is not a trio.lowlevel.checkpoint, and there are no uncheckpointed statements, check if there is a lowlevel checkpoint in the special list. If so, raise a TRIO912 for it and remove it.

    def leave_Await(
        self, original_node: cst.Await, updated_node: cst.Await
    ) -> cst.Await:
        # the expression being awaited is not checkpointed
        # so only set checkpoint after the await node

        # TODO: dirty hack to get identity right, the logic in visit should maybe be
        # moved/split into the leave
        if original_node in self.lowlevel_checkpoints:
            self.lowlevel_checkpoint_updated_nodes[original_node] = updated_node

        # all nodes are now checkpointed
        self.uncheckpointed_statements = set()
        return updated_node

    # raising exception means we don't need to checkpoint so we can treat it as one
    # can't use TypeVar due to libcst's built-in type checking not supporting it
    leave_Raise = leave_Await  # type: ignore

    # Async context managers can reasonably checkpoint on either or both of entry and
    # exit.  Given that we can't tell which, we assume "both" to avoid raising a
    # missing-checkpoint warning when there might in fact be one (i.e. a false alarm).
    def visit_With_body(self, node: cst.With):
        if getattr(node, "asynchronous", None):
            self.uncheckpointed_statements = set()

    leave_With_body = visit_With_body

    # error if no checkpoint since earlier yield or function entry
    def leave_Yield(
        self, original_node: cst.Yield, updated_node: cst.Yield
    ) -> cst.Yield:
        if not self.async_function:
            return updated_node
        self.has_yield = True

        if self.check_function_exit(original_node) and not self.noautofix:
            self.add_statement = self.checkpoint_statement()

        # mark as requiring checkpoint after
        pos = self.get_metadata(PositionProvider, original_node).start
        self.uncheckpointed_statements = {Statement("yield", pos.line, pos.column)}
        # return original to avoid problems with identity equality
        assert original_node.deep_equals(updated_node)
        return original_node

    # valid checkpoint if there's valid checkpoints (or raise) in:
    # (try or else) and all excepts, or in finally
    #
    # try can jump into any except or into the finally* at any point during it's
    # execution so we need to make sure except & finally can handle worst-case
    # * unless there's a bare except / except BaseException - not implemented.
    def visit_Try(self, node: cst.Try):
        if not self.async_function:
            return
        self.save_state(node, "try_state")
        # except & finally guaranteed to enter with checkpoint if checkpointed
        # before try and no yield in try body.
        self.try_state.body_uncheckpointed_statements = (
            self.uncheckpointed_statements.copy()
        )
        # yields inside `try` can always be uncheckpointed
        for inner_node in m.findall(node.body, m.Yield()):
            pos = self.get_metadata(PositionProvider, inner_node).start
            self.try_state.body_uncheckpointed_statements.add(
                Statement("yield", pos.line, pos.column)
            )

    def leave_Try_body(self, node: cst.Try):
        # save state at end of try for entering else
        self.try_state.try_checkpoint = self.uncheckpointed_statements

        # check that all except handlers checkpoint (await or most likely raise)
        self.try_state.except_uncheckpointed_statements = set()

    def visit_ExceptHandler(self, node: cst.ExceptHandler):
        # enter with worst case of try
        self.uncheckpointed_statements = (
            self.try_state.body_uncheckpointed_statements.copy()
        )

    def leave_ExceptHandler(
        self, original_node: cst.ExceptHandler, updated_node: cst.ExceptHandler
    ) -> cst.ExceptHandler:
        self.try_state.except_uncheckpointed_statements.update(
            self.uncheckpointed_statements
        )
        return updated_node

    def visit_Try_orelse(self, node: cst.Try):
        # check else
        # if else runs it's after all of try, so restore state to back then
        self.uncheckpointed_statements = self.try_state.try_checkpoint

    def leave_Try_orelse(self, node: cst.Try):
        # checkpoint if else checkpoints, and all excepts checkpoint
        self.uncheckpointed_statements.update(
            self.try_state.except_uncheckpointed_statements
        )

    def visit_Try_finalbody(self, node: cst.Try):
        if node.finalbody:
            self.try_state.added = (
                self.try_state.body_uncheckpointed_statements.difference(
                    self.uncheckpointed_statements
                )
            )
            # if there's no bare except or except BaseException, we can jump into
            # finally from any point in try. But the exception will be reraised after
            # finally, so track what we add so it can be removed later.
            # (This is for catching return or yield in the finally, which is usually
            # very bad)
            if not any(
                h.type is None
                or (isinstance(h.type, cst.Name) and h.type.value == "BaseException")
                for h in node.handlers
            ):
                self.uncheckpointed_statements.update(self.try_state.added)

    def leave_Try_finalbody(self, node: cst.Try):
        if node.finalbody:
            self.uncheckpointed_statements.difference_update(self.try_state.added)

    def leave_Try(self, original_node: cst.Try, updated_node: cst.Try) -> cst.Try:
        self.restore_state(original_node)
        return updated_node

    # if a previous lowlevel checkpoint is marked as redundant after all bodies, then
    # it's redundant.
    # If any body marks it as necessary, then it's necessary.
    # Otherwise, it keeps it's state from before.
    def leave_If_test(self, node: cst.If | cst.IfExp) -> None:
        if not self.async_function:
            return
        self.save_state(node, "uncheckpointed_statements", copy=True)

    def leave_If_body(self, node: cst.If | cst.IfExp) -> None:
        if not self.async_function:
            return

        # restore state to after test, saving current state instead
        (
            self.uncheckpointed_statements,
            self.outer[node]["uncheckpointed_statements"],
        ) = (
            self.outer[node]["uncheckpointed_statements"],
            self.uncheckpointed_statements,
        )

    def leave_If(self, original_node: cst.If, updated_node: cst.If) -> cst.If:
        if self.async_function:
            # merge current state with post-body state
            self.uncheckpointed_statements.update(
                self.outer[original_node]["uncheckpointed_statements"]
            )
        return updated_node

    # libcst calls attributes in the order they appear in the code, so we manually
    # rejig the order here
    def visit_IfExp(self, node: cst.IfExp) -> bool:
        _ = node.test.visit(self)
        self.leave_If_test(node)
        _ = node.body.visit(self)
        self.leave_If_body(node)
        _ = node.orelse.visit(self)
        self.leave_If(node, node)  # type: ignore
        return False  # libcst shouldn't visit subnodes again

    def visit_While(self, node: cst.While | cst.For):
        self.save_state(
            node,
            "loop_state",
            copy=True,
        )
        self.loop_state = LoopState()
        self.loop_state.infinite_loop = self.loop_state.body_guaranteed_once = False

    visit_For = visit_While

    # Check for yields w/o checkpoint in between due to entering loop body the first time,
    # after completing all of loop body, and after any continues.
    # yield in else have same requirement
    # state after the loop same as above, and in addition the state at any break
    def visit_While_test(self, node: cst.While):
        # save state in case of nested loops
        # One could plausibly just check for True here
        if (m.matches(node.test, m.Name("True"))) or (
            getattr(node.test, "evaluated_value", False)
        ):
            self.loop_state.infinite_loop = self.loop_state.body_guaranteed_once = True

    def visit_For_iter(self, node: cst.For):
        self.loop_state.body_guaranteed_once = iter_guaranteed_once_cst(node.iter)

    def visit_While_body(self, node: cst.For | cst.While):
        if not self.async_function:
            return

        self.save_state(
            node,
            "uncheckpointed_statements",
        )

        # inject an artificial uncheckpointed statement that won't raise an error,
        # but will be marked if an error would be generated. We can then generate
        # appropriate errors if the loop doesn't checkpoint

        if getattr(node, "asynchronous", None):
            self.uncheckpointed_statements = set()
        else:
            self.uncheckpointed_statements = {ARTIFICIAL_STATEMENT}

        self.loop_state.uncheckpointed_before_continue = set()
        self.loop_state.uncheckpointed_before_break = set()

    visit_For_body = visit_While_body

    def leave_While_body(self, node: cst.For | cst.While):
        if not self.async_function:
            return

        # if there's errors due to the artificial statement
        # raise a real error for each statement in outer[uncheckpointed_statements],
        # uncheckpointed_before_continue, and checkpoints at the end of the loop
        any_error = False
        for err_node in self.loop_state.artificial_errors:
            for stmt in (
                self.outer[node]["uncheckpointed_statements"]
                | self.uncheckpointed_statements
                | self.loop_state.uncheckpointed_before_continue
            ):
                if stmt == ARTIFICIAL_STATEMENT:
                    continue
                any_error = True
                self.error_91x(err_node, stmt)

        # if there's no errors from artificial statements, we don't need to insert
        # the potential checkpoints
        if not any_error:
            self.loop_state.nodes_needing_checkpoints = []

            # but lowlevel checkpoints are redundant
            for expr in self.loop_state.possibly_redundant_lowlevel_checkpoints:
                self.error(expr, error_code="TRIO912")
            # self.possibly_redundant_lowlevel_checkpoints.clear()

        # replace artificial statements in else with prebody uncheckpointed statements
        # non-artificial stmts before continue/break/at body end will already be in them
        for stmts in (
            self.loop_state.uncheckpointed_before_continue,
            self.loop_state.uncheckpointed_before_break,
            self.uncheckpointed_statements,
        ):
            if ARTIFICIAL_STATEMENT in stmts:
                stmts.remove(ARTIFICIAL_STATEMENT)
                stmts.update(self.outer[node]["uncheckpointed_statements"])

        # AsyncFor guarantees checkpoint on running out of iterable
        # so reset checkpoint state at end of loop. (but not state at break)
        if getattr(node, "asynchronous", None):
            self.uncheckpointed_statements = set()
        else:
            # enter orelse with worst case:
            # loop body might execute fully before entering orelse
            # (current state of self.uncheckpointed_statements)
            # or not at all
            if not self.loop_state.body_guaranteed_once:
                self.uncheckpointed_statements.update(
                    self.outer[node]["uncheckpointed_statements"]
                )
            # or at a continue, unless it's an infinite loop
            if not self.loop_state.infinite_loop:
                self.uncheckpointed_statements.update(
                    self.loop_state.uncheckpointed_before_continue
                )

    leave_For_body = leave_While_body

    def leave_While_orelse(self, node: cst.For | cst.While):
        if not self.async_function:
            return
        # if this is an infinite loop, with no break in it, don't raise
        # alarms about the state after it.
        if self.loop_state.infinite_loop and not self.loop_state.has_break:
            self.uncheckpointed_statements = set()
        else:
            # We may exit from:
            # orelse (covering: no body, body until continue, and all body)
            # break
            self.uncheckpointed_statements.update(
                self.loop_state.uncheckpointed_before_break
            )

        # reset break & continue in case of nested loops
        self.outer[node]["uncheckpointed_statements"] = self.uncheckpointed_statements

        # TODO: if this loop always checkpoints
        # e.g. from being an async for, or being guaranteed to run once, or other stuff.
        # then we can warn about redundant checkpoints before the loop.
        # ... except if the reason we always checkpoint is due to redundant checkpoints
        # we're about to remove.... :thinking:

    leave_For_orelse = leave_While_orelse

    def leave_While(
        self, original_node: cst.For | cst.While, updated_node: cst.For | cst.While
    ) -> (
        cst.While
        | cst.For
        | cst.FlattenSentinel[cst.For | cst.While]
        | cst.RemovalSentinel
    ):
        if self.loop_state.nodes_needing_checkpoints and self.should_autofix():
            transformer = InsertCheckpointsInLoopBody(
                self.loop_state.nodes_needing_checkpoints, self.library
            )
            # type of updated_node expanded to the return type
            updated_node = updated_node.visit(transformer)  # type: ignore

        self.restore_state(original_node)
        # https://github.com/afonasev/flake8-return/issues/133
        return updated_node  # noqa: R504

    leave_For = leave_While

    # save state in case of continue/break at a point not guaranteed to checkpoint
    def visit_Continue(self, node: cst.Continue):
        if not self.async_function:
            return
        self.loop_state.uncheckpointed_before_continue.update(
            self.uncheckpointed_statements
        )

    def visit_Break(self, node: cst.Break):
        self.loop_state.has_break = True
        if not self.async_function:
            return
        self.loop_state.uncheckpointed_before_break.update(
            self.uncheckpointed_statements
        )

    # we visit BooleanOperation_left as usual, but ignore checkpoints in the
    # right-hand side while still adding any yields in it.
    def visit_BooleanOperation_right(self, node: cst.BooleanOperation):
        if not self.async_function:
            return
        self.save_state(node, "uncheckpointed_statements", copy=True)

    def leave_BooleanOperation_right(self, node: cst.BooleanOperation):
        if not self.async_function:
            return
        self.uncheckpointed_statements.update(
            self.outer[node]["uncheckpointed_statements"]
        )

    # comprehensions are simpler than loops, since they cannot contain yields
    # or many other complicated statements, but their subfields are not in the order
    # they're logically executed, so we manually visit each field in execution order,
    # as long as the effect of the statement is not known. Once we know the comprehension
    # will checkpoint, we stop visiting, or once we are no longer guaranteed to execute
    # code deeper in the comprehension.
    # Functions return `False` so libcst doesn't iterate subnodes [again].
    def visit_ListComp(self, node: cst.DictComp | cst.SetComp | cst.ListComp):
        if not self.async_function or not self.uncheckpointed_statements:
            return False

        outer = self.comp_unknown
        self.comp_unknown = True

        # visit `for` and `if`s
        node.for_in.visit(self)

        # if still unknown, visit the expression
        if self.comp_unknown and self.uncheckpointed_statements:
            if isinstance(node, cst.DictComp):
                node.key.visit(self)
                node.value.visit(self)
            else:
                node.elt.visit(self)

        self.comp_unknown = outer
        return False

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp

    def visit_CompFor(self, node: cst.CompFor):
        # should only ever be visited manually, when inside an async function
        assert self.async_function

        if not self.uncheckpointed_statements:
            return False

        # if async comprehension, checkpoint
        if node.asynchronous:
            self.uncheckpointed_statements = set()
            self.comp_unknown = False
            return False

        # visit the iter call, which might have await's
        node.iter.visit(self)

        # stop checking if the loop is not guaranteed to execute
        if not iter_guaranteed_once_cst(node.iter):
            self.comp_unknown = False

        # only the first `if` is guaranteed to execute
        # and if there's any, don't check inner loop
        elif node.ifs:
            self.comp_unknown = False
            node.ifs[0].visit(self)
        elif node.inner_for_in:
            # visit nested loops (and ifs), if any
            node.inner_for_in.visit(self)

        return False

    # The generator target will be immediately evaluated, but the other
    # elements will be lazily evaluated as the generator is consumed so we don't
    # visit them as any checkpoints in them are not guaranteed to execute.
    def visit_GeneratorExp(self, node: cst.GeneratorExp):
        node.for_in.iter.visit(self)
        return False

    def visit_Import(self, node: cst.Import):
        """Register explicitly imported library.

        This is *slightly* different enough from what the utility visitor does
        that it's added here, but the functionality is maybe better placed in there.
        """
        for alias in node.names:
            if m.matches(
                alias, m.ImportAlias(name=m.Name("trio") | m.Name("anyio"), asname=None)
            ):
                assert isinstance(alias.name.value, str)
                self.explicitly_imported_library[alias.name.value] = True

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module):
        """Add needed library import, if any, to the module."""
        if not self.add_import:
            return updated_node

        # make sure we insert the import after docstring[s], comments at the beginning of
        # the file, and after other imports (especially important to be after __future__)
        new_body = list(updated_node.body)
        index = 0
        while m.matches(
            new_body[index],
            m.SimpleStatementLine(
                [m.ImportFrom() | m.Import() | m.Expr(m.SimpleString())]
            ),
        ):
            index += 1
        # trivial to insert multiple imports - but it should not happen with current
        # implementation
        assert len(self.add_import) == 1
        new_body.insert(index, cst.parse_statement(f"import {self.library[0]}"))
        return updated_node.with_changes(body=new_body)
