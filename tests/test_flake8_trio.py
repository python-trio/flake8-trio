import ast
import copy
import os
import re
import site
import sys
import unittest
from pathlib import Path
from typing import DefaultDict, Iterable, List, Sequence, Tuple

import pytest

# import trio  # type: ignore
from hypothesis import HealthCheck, given, settings
from hypothesmith import from_grammar, from_node

from flake8_trio import Error, Error_codes, Plugin, Statement

test_files: List[Tuple[str, str]] = sorted(
    (os.path.splitext(f)[0].upper(), f)
    for f in os.listdir("tests")
    if re.match(r"trio.*.py", f)
)


class ParseError(Exception):
    ...


@pytest.mark.parametrize("test, path", test_files)
def test_eval(test: str, path: str):
    # version check
    python_version = re.search(r"(?<=_PY)\d*", test)
    if python_version:
        version_str = python_version.group()
        major, minor = version_str[0], version_str[1:]
        v_i = sys.version_info
        if (v_i.major, v_i.minor) < (int(major), int(minor)):
            raise unittest.SkipTest("v_i, major, minor")
        test = test.split("_")[0]

    assert test in Error_codes.keys(), "error code not defined in flake8_trio.py"

    include = [test]
    expected: List[Error] = []
    with open(os.path.join("tests", path)) as file:
        lines = file.readlines()

    for lineno, line in enumerate(lines, start=1):
        line = line.strip()

        if reg_match := re.search(r"(?<=INCLUDE).*", line):
            for other_code in reg_match.group().split(" "):
                if other_code.strip():
                    include.append(other_code.strip())

        # skip commented out lines
        if not line or line[0] == "#":
            continue

        # get text between `error:` and (end of line or another comment)
        k = re.findall(r"(?<=error:)[^#]*(?=#|$)", line)

        for reg_match in k:
            try:
                # Append a bunch of empty strings so string formatting gives garbage
                # instead of throwing an exception
                args = eval(
                    f"[{reg_match}]",
                    {
                        "lineno": lineno,
                        "line": lineno,
                        "Statement": Statement,
                        "Stmt": Statement,
                    },
                )

            except Exception as e:
                print(f"lineno: {lineno}, line: {line}", file=sys.stderr)
                raise e
            col, *args = args
            assert isinstance(
                col, int
            ), f'invalid column "{col}" @L{lineno}, in "{line}"'

            # assert col.isdigit(), f'invalid column "{col}" @L{lineno}, in "{line}"'
            try:
                expected.append(Error(test, lineno, int(col), *args))
            except AttributeError as e:
                msg = f'Line {lineno}: Failed to format\n "{Error_codes[test]}"\nwith\n{args}'
                raise ParseError(msg) from e

    assert expected, f"failed to parse any errors in file {path}"
    assert_expected_errors(path, include, *expected)


def assert_expected_errors(test_file: str, include: Iterable[str], *expected: Error):
    filename = Path(__file__).absolute().parent / test_file
    plugin = Plugin.from_filename(str(filename))

    errors = sorted(e for e in plugin.run() if e.code in include)
    expected_ = sorted(expected)

    print_first_diff(errors, expected_)
    assert_correct_lines_and_codes(errors, expected_)
    assert_correct_columns(errors, expected_)
    assert_correct_args(errors, expected_)

    # full check
    unittest.TestCase().assertEqual(errors, expected_)

    # test tuple conversion and iter types
    assert_tuple_and_types(errors, expected_)


def print_first_diff(errors: Sequence[Error], expected: Sequence[Error]):
    first_error_line: List[Error] = []
    for e in errors:
        if e.line == errors[0].line:
            first_error_line.append(e)
    first_expected_line: List[Error] = []
    for e in expected:
        if e.line == expected[0].line:
            first_expected_line.append(e)
    if first_expected_line != first_error_line:
        print(
            "First lines with different errors",
            f"  actual: {[e.cmp() for e in first_error_line]}",
            f"expected: {[e.cmp() for e in first_expected_line]}",
            "",
            sep="\n",
            file=sys.stderr,
        )


def assert_correct_lines_and_codes(errors: Iterable[Error], expected: Iterable[Error]):
    MyDict = DefaultDict[int, DefaultDict[str, int]]
    # Check that errors are on correct lines
    all_lines = sorted({e.line for e in (*errors, *expected)})
    error_dict: MyDict = DefaultDict(lambda: DefaultDict(int))
    expected_dict = copy.deepcopy(error_dict)

    for e in errors:
        error_dict[e.line][e.code] += 1
    for e in expected:
        expected_dict[e.line][e.code] += 1

    any_error = False
    for line in all_lines:
        if error_dict[line] == expected_dict[line]:
            continue
        for code in {*error_dict[line], *expected_dict[line]}:
            if not any_error:
                print(
                    "Lines with different # of errors:",
                    "-" * 38,
                    f"| line | {'code':7} | actual | expected |",
                    sep="\n",
                    file=sys.stderr,
                )
                any_error = True

            print(
                f"| {line:4}",
                f"{code}",
                f"{error_dict[line][code]:6}",
                f"{expected_dict[line][code]:8} |",
                sep=" | ",
                file=sys.stderr,
            )
    assert not any_error


def assert_correct_columns(errors: Iterable[Error], expected: Iterable[Error]):
    # check errors have correct columns
    col_error = False
    for err, exp in zip(errors, expected):
        assert err.line == exp.line
        if err.col != exp.col:
            if not col_error:
                print("Errors with same line but different columns:", file=sys.stderr)
                print("| line | actual | expected |", file=sys.stderr)
                col_error = True
            print(
                f"| {err.line:4} | {err.col:6} | {exp.col:8} |",
                file=sys.stderr,
            )
    assert not col_error


def assert_correct_args(errors: Iterable[Error], expected: Iterable[Error]):
    # check errors have correct messages
    args_error = False
    for err, exp in zip(errors, expected):
        assert (err.line, err.col, err.code) == (exp.line, exp.col, exp.code)
        if err.args != exp.args:
            if not args_error:
                print(
                    "Errors with different args:",
                    "-" * 20,
                    sep="\n",
                    file=sys.stderr,
                )
                args_error = True
            print(
                f"*    line: {err.line:3} differs\n",
                f"  actual: {err.args}\n",
                f"expected: {exp.args}\n",
                "-" * 20,
                file=sys.stderr,
            )
    assert not args_error


def assert_tuple_and_types(errors: Iterable[Error], expected: Iterable[Error]):
    def info_tuple(error: Error):
        try:
            return tuple(error)
        except IndexError:
            print(
                "Failed to format error message",
                f"line: {error.line}",
                f"col: {error.col}",
                f"code: {error.code}",
                f"args: {error.args}",
                f'format string: "{Error_codes[error.code]}"',
                sep="\n    ",
                file=sys.stderr,
            )
            raise

    for err, exp in zip(errors, expected):
        err_msg = info_tuple(err)
        for err, type_ in zip(err_msg, (int, int, str, type)):
            assert isinstance(err, type_)
        assert err_msg == info_tuple(exp)


@pytest.mark.fuzz
class TestFuzz(unittest.TestCase):
    @settings(max_examples=1_000, suppress_health_check=[HealthCheck.too_slow])
    @given((from_grammar() | from_node()).map(ast.parse))
    def test_does_not_crash_on_any_valid_code(self, syntax_tree: ast.AST):
        # Given any syntatically-valid source code, the checker should
        # not crash.  This tests doesn't check that we do the *right* thing,
        # just that we don't crash on valid-if-poorly-styled code!
        Plugin(syntax_tree).run()

    @staticmethod
    def _iter_python_files():
        # Because the generator isn't perfect, we'll also test on all the code
        # we can easily find in our current Python environment - this includes
        # the standard library, and all installed packages.
        for base in sorted(set(site.PREFIXES)):
            for dirname, _, files in os.walk(base):
                for f in files:
                    if f.endswith(".py"):
                        yield Path(dirname) / f

    def test_does_not_crash_on_site_code(self):
        for path in self._iter_python_files():
            try:
                Plugin.from_filename(str(path)).run()
            except Exception as err:
                raise AssertionError(f"Failed on {path}") from err
