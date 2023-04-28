# AUTOFIX
# ARG --enable=TRIO910,TRIO911,TRIO912
import trio
import trio.lowlevel
from typing import Any


async def foo() -> Any:
    await trio.lowlevel.checkpoint()


async def sequence_00():
    await trio.lowlevel.checkpoint()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequence_01():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await foo()


async def sequence_10():
    await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequence_11():
    await foo()
    await foo()


# all permutations of 3
async def sequencing_000():
    await trio.lowlevel.checkpoint()
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequencing_001():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await foo()


async def sequencing_010():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequencing_011():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await foo()
    await foo()


async def sequencing_100():
    await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequencing_101():
    await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    await foo()


async def sequencing_110():
    await foo()
    await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def sequencing_111():
    await foo()
    await foo()
    await foo()


# when entering an if statement, there's 3 possible states:
# there's uncheckpointed statements
# checkpointed by lowlevel
# checkpointed by non-lowlevel


# we need to determined whether to treat the if statement as a lowlevel checkpoint,
# a non-lowlevel checkpoint, or not checkpointing. Both w/r/t statements before it, and
# separately w/r/t statements after it.
# and we also need to handle redundant checkpoints within bodies of it.

# the if statement can:

# 1. not checkpoint at all, easy
# 2. checkpoint in all branches with lowlevel, in which case they can all be removed
# 3. checkpoint in at least some branches with non-lowlevel.


async def foo_if():
    if ...:
        await trio.lowlevel.checkpoint()
    else:
        await foo()


async def foo_if_2():
    if ...:
        await trio.lowlevel.checkpoint()
    else:
        await trio.lowlevel.checkpoint()


async def foo_if_3():
    await trio.lowlevel.checkpoint()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8


async def foo_if_4():
    if ...:
        await trio.lowlevel.checkpoint()
    else:
        await trio.lowlevel.checkpoint()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_5():
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await foo()


async def foo_if_0000():
    await trio.lowlevel.checkpoint()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_0001():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await foo()


async def foo_if_0010():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8 # INCORRECT
    else:
        await foo()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_0100():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    if ...:
        await foo()
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8 # INCORRECT
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_1000():
    await foo()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_1000_1():
    await foo()
    yield
    if ...:
        await trio.lowlevel.checkpoint()
    else:
        await trio.lowlevel.checkpoint()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_1000_2():
    await foo()
    if ...:
        yield
        await trio.lowlevel.checkpoint()
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_1000_3():
    await foo()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
        yield
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await trio.lowlevel.checkpoint()


async def foo_if_1000_4():
    await foo()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        yield
        await trio.lowlevel.checkpoint()
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_if_1000_5():
    await foo()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
        yield
    await trio.lowlevel.checkpoint()


async def foo_if_1000_6():
    await foo()
    if ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    else:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    yield
    await trio.lowlevel.checkpoint()


async def foo_while_1():
    await trio.lowlevel.checkpoint()
    while ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_while_2():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    while ...:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    await foo()


async def foo_while_3():
    await trio.lowlevel.checkpoint()
    while ...:
        if ...:
            await trio.lowlevel.checkpoint()  # TRIO912: 12
        elif ...:
            await trio.lowlevel.checkpoint()  # TRIO912: 12
        else:
            await trio.lowlevel.checkpoint()  # TRIO912: 12

    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_while_4():
    await trio.lowlevel.checkpoint()  # should be 912
    while ...:
        if ...:
            await foo()
        # and these probably shouldn't be?
        elif ...:
            await trio.lowlevel.checkpoint()  # TRIO912: 12
        else:
            await trio.lowlevel.checkpoint()  # TRIO912: 12

    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_while_5():
    await trio.lowlevel.checkpoint()  # should be TRIO912
    while ...:
        await foo()

    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_while_6():
    await trio.lowlevel.checkpoint()  # should error
    while ...:
        if ...:
            await foo()
        elif ...:
            await foo()
        else:
            await foo()

    await trio.lowlevel.checkpoint()  # TRIO912: 4


async def foo_trio_1():
    await trio.lowlevel.checkpoint()
    try:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    except:
        await trio.lowlevel.checkpoint()  # TRIO912: 8


async def foo_trio_2():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    try:
        await trio.lowlevel.checkpoint()  # TRIO912: 8 # INCORRECT
    except:
        await foo()


async def foo_trio_3():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    try:
        await foo()
    except:
        await trio.lowlevel.checkpoint()  # TRIO912: 8 # INCORRECT


async def foo_trio_4():
    await trio.lowlevel.checkpoint()  # TRIO912: 4
    try:
        await foo()
    except:
        await foo()


async def foo_trio_5():
    await foo()
    try:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    except:
        await trio.lowlevel.checkpoint()  # TRIO912: 8


async def foo_trio_6():
    await foo()
    try:
        await trio.lowlevel.checkpoint()  # TRIO912: 8
    except:
        await foo()


async def foo_trio_7():
    await foo()
    try:
        await foo()
    except:
        await trio.lowlevel.checkpoint()  # TRIO912: 8


async def foo_trio_8():
    await foo()
    try:
        await foo()
    except:
        await foo()
