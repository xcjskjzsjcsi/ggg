import enum
import json
import operator
import re
import time
import uuid
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from random import randrange
from typing import (
    Annotated,
    Any,
    Dict,
    Generator,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
    cast,
    get_type_hints,
)

import httpx
import pytest
from langchain_core.runnables import (
    RunnableConfig,
    RunnableLambda,
    RunnableMap,
    RunnablePassthrough,
    RunnablePick,
)
from langsmith import traceable
from pydantic import BaseModel
from pytest_mock import MockerFixture
from syrupy import SnapshotAssertion

from langgraph.channels.base import BaseChannel
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.channels.context import Context
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.channels.last_value import LastValue
from langgraph.channels.topic import Topic
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import ERROR, PULL, PUSH
from langgraph.errors import InvalidUpdateError, MultipleSubgraphsError, NodeInterrupt
from langgraph.graph import END, Graph
from langgraph.graph.graph import START
from langgraph.graph.message import MessageGraph, add_messages
from langgraph.graph.state import StateGraph
from langgraph.managed.shared_value import SharedValue
from langgraph.prebuilt.chat_agent_executor import (
    create_tool_calling_executor,
)
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.pregel import (
    Channel,
    GraphRecursionError,
    Pregel,
    StateSnapshot,
)
from langgraph.pregel.retry import RetryPolicy
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.types import Interrupt, PregelTask, Send, StreamWriter
from tests.any_str import AnyDict, AnyStr, AnyVersion, FloatBetween, UnsortedSequence
from tests.conftest import (
    ALL_CHECKPOINTERS_SYNC,
    ALL_STORES_SYNC,
    SHOULD_CHECK_SNAPSHOTS,
)
from tests.fake_chat import FakeChatModel
from tests.fake_tracer import FakeTracer
from tests.memory_assert import MemorySaverAssertCheckpointMetadata
from tests.messages import (
    _AnyIdAIMessage,
    _AnyIdAIMessageChunk,
    _AnyIdHumanMessage,
    _AnyIdToolMessage,
)


# define these objects to avoid importing langchain_core.agents
# and therefore avoid relying on core Pydantic version
class AgentAction(BaseModel):
    tool: str
    tool_input: Union[str, dict]
    log: str
    type: Literal["AgentAction"] = "AgentAction"

    model_config = {
        "json_schema_extra": {
            "description": (
                """Represents a request to execute an action by an agent.

The action consists of the name of the tool to execute and the input to pass
to the tool. The log is used to pass along extra information about the action."""
            )
        }
    }


class AgentFinish(BaseModel):
    """Final return value of an ActionAgent.

    Agents return an AgentFinish when they have reached a stopping condition.
    """

    return_values: dict
    log: str
    type: Literal["AgentFinish"] = "AgentFinish"
    model_config = {
        "json_schema_extra": {
            "description": (
                """Final return value of an ActionAgent.

Agents return an AgentFinish when they have reached a stopping condition."""
            )
        }
    }


def test_graph_validation() -> None:
    def logic(inp: str) -> str:
        return ""

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.set_entry_point("agent")
    workflow.set_finish_point("agent")
    assert workflow.compile(), "valid graph"

    # Accept a dead-end
    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.set_entry_point("agent")
    workflow.compile()

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.set_finish_point("agent")
    with pytest.raises(ValueError, match="not reachable"):
        workflow.compile()

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", logic, {"continue": "tools", "exit": END})
    workflow.add_edge("tools", "agent")
    assert workflow.compile(), "valid graph"

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    workflow.set_entry_point("tools")
    workflow.add_conditional_edges("agent", logic, {"continue": "tools", "exit": END})
    workflow.add_edge("tools", "agent")
    assert workflow.compile(), "valid graph"

    workflow = Graph()
    workflow.set_entry_point("tools")
    workflow.add_conditional_edges("agent", logic, {"continue": "tools", "exit": END})
    workflow.add_edge("tools", "agent")
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    assert workflow.compile(), "valid graph"

    workflow = Graph()
    workflow.set_entry_point("tools")
    workflow.add_conditional_edges(
        "agent", logic, {"continue": "tools", "exit": END, "hmm": "extra"}
    )
    workflow.add_edge("tools", "agent")
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    with pytest.raises(ValueError, match="unknown"):  # extra is not defined
        workflow.compile()

    workflow = Graph()
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", logic, {"continue": "tools", "exit": END})
    workflow.add_edge("tools", "extra")
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    with pytest.raises(ValueError, match="unknown"):  # extra is not defined
        workflow.compile()

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    workflow.add_node("extra", logic)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", logic, {"continue": "tools", "exit": END})
    workflow.add_edge("tools", "agent")
    with pytest.raises(
        ValueError, match="Node `extra` is not reachable"
    ):  # extra is not reachable
        workflow.compile()

    workflow = Graph()
    workflow.add_node("agent", logic)
    workflow.add_node("tools", logic)
    workflow.add_node("extra", logic)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", logic)
    workflow.add_edge("tools", "agent")
    # Accept, even though extra is dead-end
    workflow.compile()

    class State(TypedDict):
        hello: str

    def node_a(state: State) -> State:
        # typo
        return {"hell": "world"}

    builder = StateGraph(State)
    builder.add_node("a", node_a)
    builder.set_entry_point("a")
    builder.set_finish_point("a")
    graph = builder.compile()
    with pytest.raises(InvalidUpdateError):
        graph.invoke({"hello": "there"})

    graph = StateGraph(State)
    graph.add_node("start", lambda x: x)
    graph.add_edge("__start__", "start")
    graph.add_edge("unknown", "start")
    graph.add_edge("start", "__end__")
    with pytest.raises(ValueError, match="Found edge starting at unknown node "):
        graph.compile()

    def bad_reducer(a): ...

    class BadReducerState(TypedDict):
        hello: Annotated[str, bad_reducer]

    with pytest.raises(ValueError, match="Invalid reducer"):
        StateGraph(BadReducerState)

    def node_b(state: State) -> State:
        return {"hello": "world"}

    builder = StateGraph(State)
    builder.add_node("a", node_b)
    builder.add_node("b", node_b)
    builder.add_node("c", node_b)
    builder.set_entry_point("a")
    builder.add_edge("a", "b")
    builder.add_edge("a", "c")
    graph = builder.compile()

    with pytest.raises(InvalidUpdateError, match="At key 'hello'"):
        graph.invoke({"hello": "there"})


def test_checkpoint_errors() -> None:
    class FaultyGetCheckpointer(MemorySaver):
        def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
            raise ValueError("Faulty get_tuple")

    class FaultyPutCheckpointer(MemorySaver):
        def put(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: Optional[dict[str, Union[str, int, float]]] = None,
        ) -> RunnableConfig:
            raise ValueError("Faulty put")

    class FaultyPutWritesCheckpointer(MemorySaver):
        def put_writes(
            self, config: RunnableConfig, writes: List[Tuple[str, Any]], task_id: str
        ) -> RunnableConfig:
            raise ValueError("Faulty put_writes")

    class FaultyVersionCheckpointer(MemorySaver):
        def get_next_version(self, current: Optional[int], channel: BaseChannel) -> int:
            raise ValueError("Faulty get_next_version")

    def logic(inp: str) -> str:
        return ""

    builder = StateGraph(Annotated[str, operator.add])
    builder.add_node("agent", logic)
    builder.add_edge(START, "agent")

    graph = builder.compile(checkpointer=FaultyGetCheckpointer())
    with pytest.raises(ValueError, match="Faulty get_tuple"):
        graph.invoke("", {"configurable": {"thread_id": "thread-1"}})

    graph = builder.compile(checkpointer=FaultyPutCheckpointer())
    with pytest.raises(ValueError, match="Faulty put"):
        graph.invoke("", {"configurable": {"thread_id": "thread-1"}})

    graph = builder.compile(checkpointer=FaultyVersionCheckpointer())
    with pytest.raises(ValueError, match="Faulty get_next_version"):
        graph.invoke("", {"configurable": {"thread_id": "thread-1"}})

    # add parallel node
    builder.add_node("parallel", logic)
    builder.add_edge(START, "parallel")
    graph = builder.compile(checkpointer=FaultyPutWritesCheckpointer())
    with pytest.raises(ValueError, match="Faulty put_writes"):
        graph.invoke("", {"configurable": {"thread_id": "thread-1"}})


def test_node_schemas_custom_output() -> None:
    class State(TypedDict):
        hello: str
        bye: str
        messages: Annotated[list[str], add_messages]

    class Output(TypedDict):
        messages: list[str]

    class StateForA(TypedDict):
        hello: str
        messages: Annotated[list[str], add_messages]

    def node_a(state: StateForA) -> State:
        assert state == {
            "hello": "there",
            "messages": [_AnyIdHumanMessage(content="hello")],
        }

    class StateForB(TypedDict):
        bye: str
        now: int

    def node_b(state: StateForB):
        assert state == {
            "bye": "world",
        }
        return {
            "now": 123,
            "hello": "again",
        }

    class StateForC(TypedDict):
        hello: str
        now: int

    def node_c(state: StateForC) -> StateForC:
        assert state == {
            "hello": "again",
            "now": 123,
        }

    builder = StateGraph(State, output=Output)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_node("c", node_c)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", "c")
    graph = builder.compile()

    assert graph.invoke({"hello": "there", "bye": "world", "messages": "hello"}) == {
        "messages": [_AnyIdHumanMessage(content="hello")],
    }

    builder = StateGraph(State, output=Output)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_node("c", node_c)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", "c")
    graph = builder.compile()

    assert graph.invoke(
        {
            "hello": "there",
            "bye": "world",
            "messages": "hello",
            "now": 345,  # ignored because not in input schema
        }
    ) == {
        "messages": [_AnyIdHumanMessage(content="hello")],
    }

    assert [
        c
        for c in graph.stream(
            {
                "hello": "there",
                "bye": "world",
                "messages": "hello",
                "now": 345,  # ignored because not in input schema
            }
        )
    ] == [
        {"a": None},
        {"b": {"hello": "again", "now": 123}},
        {"c": None},
    ]


def test_reducer_before_first_node() -> None:
    class State(TypedDict):
        hello: str
        messages: Annotated[list[str], add_messages]

    def node_a(state: State) -> State:
        assert state == {
            "hello": "there",
            "messages": [_AnyIdHumanMessage(content="hello")],
        }

    builder = StateGraph(State)
    builder.add_node("a", node_a)
    builder.set_entry_point("a")
    builder.set_finish_point("a")
    graph = builder.compile()
    assert graph.invoke({"hello": "there", "messages": "hello"}) == {
        "hello": "there",
        "messages": [_AnyIdHumanMessage(content="hello")],
    }

    class State(TypedDict):
        hello: str
        messages: Annotated[List[str], add_messages]

    def node_a(state: State) -> State:
        assert state == {
            "hello": "there",
            "messages": [_AnyIdHumanMessage(content="hello")],
        }

    builder = StateGraph(State)
    builder.add_node("a", node_a)
    builder.set_entry_point("a")
    builder.set_finish_point("a")
    graph = builder.compile()
    assert graph.invoke({"hello": "there", "messages": "hello"}) == {
        "hello": "there",
        "messages": [_AnyIdHumanMessage(content="hello")],
    }

    class State(TypedDict):
        hello: str
        messages: Annotated[Sequence[str], add_messages]

    def node_a(state: State) -> State:
        assert state == {
            "hello": "there",
            "messages": [_AnyIdHumanMessage(content="hello")],
        }

    builder = StateGraph(State)
    builder.add_node("a", node_a)
    builder.set_entry_point("a")
    builder.set_finish_point("a")
    graph = builder.compile()
    assert graph.invoke({"hello": "there", "messages": "hello"}) == {
        "hello": "there",
        "messages": [_AnyIdHumanMessage(content="hello")],
    }


def test_invoke_single_process_in_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Channel.subscribe_to("input") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={
            "one": chain,
        },
        channels={
            "input": LastValue(int),
            "output": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )
    graph = Graph()
    graph.add_node("add_one", add_one)
    graph.set_entry_point("add_one")
    graph.set_finish_point("add_one")
    gapp = graph.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert app.input_schema.model_json_schema() == {
            "title": "LangGraphInput",
            "type": "integer",
        }
        assert app.output_schema.model_json_schema() == {
            "title": "LangGraphOutput",
            "type": "integer",
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # raise warnings as errors
            assert app.config_schema().model_json_schema() == {
                "properties": {},
                "title": "LangGraphConfig",
                "type": "object",
            }

    assert app.invoke(2) == 3
    assert app.invoke(2, output_keys=["output"]) == {"output": 3}
    assert repr(app), "does not raise recursion error"

    assert gapp.invoke(2, debug=True) == 3


@pytest.mark.parametrize(
    "falsy_value",
    [None, False, 0, "", [], {}, set(), frozenset(), 0.0, 0j],
)
def test_invoke_single_process_in_out_falsy_values(falsy_value: Any) -> None:
    graph = Graph()
    graph.add_node("return_falsy_const", lambda *args, **kwargs: falsy_value)
    graph.set_entry_point("return_falsy_const")
    graph.set_finish_point("return_falsy_const")
    gapp = graph.compile()
    assert gapp.invoke(1) == falsy_value


def test_invoke_single_process_in_write_kwargs(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = (
        Channel.subscribe_to("input")
        | add_one
        | Channel.write_to("output", fixed=5, output_plus_one=lambda x: x + 1)
    )

    app = Pregel(
        nodes={"one": chain},
        channels={
            "input": LastValue(int),
            "output": LastValue(int),
            "fixed": LastValue(int),
            "output_plus_one": LastValue(int),
        },
        output_channels=["output", "fixed", "output_plus_one"],
        input_channels="input",
    )

    if SHOULD_CHECK_SNAPSHOTS:
        assert app.input_schema.model_json_schema() == {
            "title": "LangGraphInput",
            "type": "integer",
        }
        assert app.output_schema.model_json_schema() == {
            "title": "LangGraphOutput",
            "type": "object",
            "properties": {
                "output": {"title": "Output", "type": "integer", "default": None},
                "fixed": {"title": "Fixed", "type": "integer", "default": None},
                "output_plus_one": {
                    "title": "Output Plus One",
                    "type": "integer",
                    "default": None,
                },
            },
        }
    assert app.invoke(2) == {"output": 3, "fixed": 5, "output_plus_one": 4}


def test_invoke_single_process_in_out_dict(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Channel.subscribe_to("input") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": chain},
        channels={"input": LastValue(int), "output": LastValue(int)},
        input_channels="input",
        output_channels=["output"],
    )

    if SHOULD_CHECK_SNAPSHOTS:
        assert app.input_schema.model_json_schema() == {
            "title": "LangGraphInput",
            "type": "integer",
        }
        assert app.output_schema.model_json_schema() == {
            "title": "LangGraphOutput",
            "type": "object",
            "properties": {
                "output": {"title": "Output", "type": "integer", "default": None}
            },
        }
    assert app.invoke(2) == {"output": 3}


def test_invoke_single_process_in_dict_out_dict(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Channel.subscribe_to("input") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": chain},
        channels={"input": LastValue(int), "output": LastValue(int)},
        input_channels=["input"],
        output_channels=["output"],
    )
    if SHOULD_CHECK_SNAPSHOTS:
        assert app.input_schema.model_json_schema() == {
            "title": "LangGraphInput",
            "type": "object",
            "properties": {
                "input": {"title": "Input", "type": "integer", "default": None}
            },
        }
        assert app.output_schema.model_json_schema() == {
            "title": "LangGraphOutput",
            "type": "object",
            "properties": {
                "output": {"title": "Output", "type": "integer", "default": None}
            },
        }
    assert app.invoke({"input": 2}) == {"output": 3}


def test_invoke_two_processes_in_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    one = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    two = Channel.subscribe_to("inbox") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "inbox": LastValue(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    assert app.invoke(2) == 4

    with pytest.raises(GraphRecursionError):
        app.invoke(2, {"recursion_limit": 1}, debug=1)

    graph = Graph()
    graph.add_node("add_one", add_one)
    graph.add_node("add_one_more", add_one)
    graph.set_entry_point("add_one")
    graph.set_finish_point("add_one_more")
    graph.add_edge("add_one", "add_one_more")
    gapp = graph.compile()

    assert gapp.invoke(2) == 4

    for step, values in enumerate(gapp.stream(2, debug=1), start=1):
        if step == 1:
            assert values == {
                "add_one": 3,
            }
        elif step == 2:
            assert values == {
                "add_one_more": 4,
            }
        else:
            assert 0, f"{step}:{values}"
    assert step == 2


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_invoke_two_processes_in_out_interrupt(
    request: pytest.FixtureRequest, checkpointer_name: str, mocker: MockerFixture
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    one = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    two = Channel.subscribe_to("inbox") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "inbox": LastValue(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
        checkpointer=checkpointer,
        interrupt_after_nodes=["one"],
    )
    thread1 = {"configurable": {"thread_id": "1"}}
    thread2 = {"configurable": {"thread_id": "2"}}

    # start execution, stop at inbox
    assert app.invoke(2, thread1) is None

    # inbox == 3
    checkpoint = checkpointer.get(thread1)
    assert checkpoint is not None
    assert checkpoint["channel_values"]["inbox"] == 3

    # resume execution, finish
    assert app.invoke(None, thread1) == 4

    # start execution again, stop at inbox
    assert app.invoke(20, thread1) is None

    # inbox == 21
    checkpoint = checkpointer.get(thread1)
    assert checkpoint is not None
    assert checkpoint["channel_values"]["inbox"] == 21

    # send a new value in, interrupting the previous execution
    assert app.invoke(3, thread1) is None
    assert app.invoke(None, thread1) == 5

    # start execution again, stopping at inbox
    assert app.invoke(20, thread2) is None

    # inbox == 21
    snapshot = app.get_state(thread2)
    assert snapshot.values["inbox"] == 21
    assert snapshot.next == ("two",)

    # update the state, resume
    app.update_state(thread2, 25, as_node="one")
    assert app.invoke(None, thread2) == 26

    # no pending tasks
    snapshot = app.get_state(thread2)
    assert snapshot.next == ()

    # list history
    history = [c for c in app.get_state_history(thread1)]
    assert history == [
        StateSnapshot(
            values={"inbox": 4, "output": 5, "input": 3},
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "step": 6, "writes": {"two": 5}},
            created_at=AnyStr(),
            parent_config=history[1].config,
        ),
        StateSnapshot(
            values={"inbox": 4, "output": 4, "input": 3},
            tasks=(PregelTask(AnyStr(), "two", (PULL, "two"), result={"output": 5}),),
            next=("two",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 5,
                "writes": {"one": None},
            },
            created_at=AnyStr(),
            parent_config=history[2].config,
        ),
        StateSnapshot(
            values={"inbox": 21, "output": 4, "input": 3},
            tasks=(PregelTask(AnyStr(), "one", (PULL, "one"), result={"inbox": 4}),),
            next=("one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "step": 4,
                "writes": {"input": 3},
            },
            created_at=AnyStr(),
            parent_config=history[3].config,
        ),
        StateSnapshot(
            values={"inbox": 21, "output": 4, "input": 20},
            tasks=(PregelTask(AnyStr(), "two", (PULL, "two")),),
            next=("two",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 3,
                "writes": {"one": None},
            },
            created_at=AnyStr(),
            parent_config=history[4].config,
        ),
        StateSnapshot(
            values={"inbox": 3, "output": 4, "input": 20},
            tasks=(PregelTask(AnyStr(), "one", (PULL, "one"), result={"inbox": 21}),),
            next=("one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "step": 2,
                "writes": {"input": 20},
            },
            created_at=AnyStr(),
            parent_config=history[5].config,
        ),
        StateSnapshot(
            values={"inbox": 3, "output": 4, "input": 2},
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "step": 1, "writes": {"two": 4}},
            created_at=AnyStr(),
            parent_config=history[6].config,
        ),
        StateSnapshot(
            values={"inbox": 3, "input": 2},
            tasks=(PregelTask(AnyStr(), "two", (PULL, "two"), result={"output": 4}),),
            next=("two",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 0,
                "writes": {"one": None},
            },
            created_at=AnyStr(),
            parent_config=history[7].config,
        ),
        StateSnapshot(
            values={"input": 2},
            tasks=(PregelTask(AnyStr(), "one", (PULL, "one"), result={"inbox": 3}),),
            next=("one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "step": -1,
                "writes": {"input": 2},
            },
            created_at=AnyStr(),
            parent_config=None,
        ),
    ]

    # re-running from any previous checkpoint should re-run nodes
    assert [c for c in app.stream(None, history[0].config, stream_mode="updates")] == []
    assert [c for c in app.stream(None, history[1].config, stream_mode="updates")] == [
        {"two": {"output": 5}},
    ]
    assert [c for c in app.stream(None, history[2].config, stream_mode="updates")] == [
        {"one": {"inbox": 4}},
        {"__interrupt__": ()},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_fork_always_re_runs_nodes(
    request: pytest.FixtureRequest, checkpointer_name: str, mocker: MockerFixture
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    add_one = mocker.Mock(side_effect=lambda _: 1)

    builder = StateGraph(Annotated[int, operator.add])
    builder.add_node("add_one", add_one)
    builder.add_edge(START, "add_one")
    builder.add_conditional_edges("add_one", lambda cnt: "add_one" if cnt < 6 else END)
    graph = builder.compile(checkpointer=checkpointer)

    thread1 = {"configurable": {"thread_id": "1"}}

    # start execution, stop at inbox
    assert [*graph.stream(1, thread1, stream_mode=["values", "updates"])] == [
        ("values", 1),
        ("updates", {"add_one": 1}),
        ("values", 2),
        ("updates", {"add_one": 1}),
        ("values", 3),
        ("updates", {"add_one": 1}),
        ("values", 4),
        ("updates", {"add_one": 1}),
        ("values", 5),
        ("updates", {"add_one": 1}),
        ("values", 6),
    ]

    # list history
    history = [c for c in graph.get_state_history(thread1)]
    assert history == [
        StateSnapshot(
            values=6,
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 5,
                "writes": {"add_one": 1},
            },
            created_at=AnyStr(),
            parent_config=history[1].config,
        ),
        StateSnapshot(
            values=5,
            tasks=(PregelTask(AnyStr(), "add_one", (PULL, "add_one"), result=1),),
            next=("add_one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 4,
                "writes": {"add_one": 1},
            },
            created_at=AnyStr(),
            parent_config=history[2].config,
        ),
        StateSnapshot(
            values=4,
            tasks=(PregelTask(AnyStr(), "add_one", (PULL, "add_one"), result=1),),
            next=("add_one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 3,
                "writes": {"add_one": 1},
            },
            created_at=AnyStr(),
            parent_config=history[3].config,
        ),
        StateSnapshot(
            values=3,
            tasks=(PregelTask(AnyStr(), "add_one", (PULL, "add_one"), result=1),),
            next=("add_one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 2,
                "writes": {"add_one": 1},
            },
            created_at=AnyStr(),
            parent_config=history[4].config,
        ),
        StateSnapshot(
            values=2,
            tasks=(PregelTask(AnyStr(), "add_one", (PULL, "add_one"), result=1),),
            next=("add_one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "step": 1,
                "writes": {"add_one": 1},
            },
            created_at=AnyStr(),
            parent_config=history[5].config,
        ),
        StateSnapshot(
            values=1,
            tasks=(PregelTask(AnyStr(), "add_one", (PULL, "add_one"), result=1),),
            next=("add_one",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
            created_at=AnyStr(),
            parent_config=history[6].config,
        ),
        StateSnapshot(
            values=0,
            tasks=(PregelTask(AnyStr(), "__start__", (PULL, "__start__"), result=1),),
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "step": -1,
                "writes": {"__start__": 1},
            },
            created_at=AnyStr(),
            parent_config=None,
        ),
    ]

    # forking from any previous checkpoint should re-run nodes
    assert [
        c for c in graph.stream(None, history[0].config, stream_mode="updates")
    ] == []
    assert [
        c for c in graph.stream(None, history[1].config, stream_mode="updates")
    ] == [
        {"add_one": 1},
    ]
    assert [
        c for c in graph.stream(None, history[2].config, stream_mode="updates")
    ] == [
        {"add_one": 1},
        {"add_one": 1},
    ]


def test_invoke_two_processes_in_dict_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    one = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    two = (
        Channel.subscribe_to("inbox")
        | RunnableLambda(add_one).batch
        | RunnablePassthrough(lambda _: time.sleep(0.1))
        | Channel.write_to("output").batch
    )

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "inbox": Topic(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels=["input", "inbox"],
        stream_channels=["output", "inbox"],
        output_channels=["output"],
    )

    # [12 + 1, 2 + 1 + 1]
    assert [
        *app.stream(
            {"input": 2, "inbox": 12}, output_keys="output", stream_mode="updates"
        )
    ] == [
        {"one": None},
        {"two": 13},
        {"two": 4},
    ]
    assert [*app.stream({"input": 2, "inbox": 12}, output_keys="output")] == [
        13,
        4,
    ]

    assert [*app.stream({"input": 2, "inbox": 12}, stream_mode="updates")] == [
        {"one": {"inbox": 3}},
        {"two": {"output": 13}},
        {"two": {"output": 4}},
    ]
    assert [*app.stream({"input": 2, "inbox": 12})] == [
        {"inbox": [3], "output": 13},
        {"output": 4},
    ]
    assert [*app.stream({"input": 2, "inbox": 12}, stream_mode="debug")] == [
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 0,
            "payload": {
                "id": AnyStr(),
                "name": "one",
                "input": 2,
                "triggers": ["input"],
            },
        },
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 0,
            "payload": {
                "id": AnyStr(),
                "name": "two",
                "input": [12],
                "triggers": ["inbox"],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 0,
            "payload": {
                "id": AnyStr(),
                "name": "one",
                "result": [("inbox", 3)],
                "error": None,
                "interrupts": [],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 0,
            "payload": {
                "id": AnyStr(),
                "name": "two",
                "result": [("output", 13)],
                "error": None,
                "interrupts": [],
            },
        },
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 1,
            "payload": {
                "id": AnyStr(),
                "name": "two",
                "input": [3],
                "triggers": ["inbox"],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 1,
            "payload": {
                "id": AnyStr(),
                "name": "two",
                "result": [("output", 4)],
                "error": None,
                "interrupts": [],
            },
        },
    ]


def test_batch_two_processes_in_out() -> None:
    def add_one_with_delay(inp: int) -> int:
        time.sleep(inp / 10)
        return inp + 1

    one = Channel.subscribe_to("input") | add_one_with_delay | Channel.write_to("one")
    two = Channel.subscribe_to("one") | add_one_with_delay | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "one": LastValue(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    assert app.batch([3, 2, 1, 3, 5]) == [5, 4, 3, 5, 7]
    assert app.batch([3, 2, 1, 3, 5], output_keys=["output"]) == [
        {"output": 5},
        {"output": 4},
        {"output": 3},
        {"output": 5},
        {"output": 7},
    ]

    graph = Graph()
    graph.add_node("add_one", add_one_with_delay)
    graph.add_node("add_one_more", add_one_with_delay)
    graph.set_entry_point("add_one")
    graph.set_finish_point("add_one_more")
    graph.add_edge("add_one", "add_one_more")
    gapp = graph.compile()

    assert gapp.batch([3, 2, 1, 3, 5]) == [5, 4, 3, 5, 7]


def test_invoke_many_processes_in_out(mocker: MockerFixture) -> None:
    test_size = 100
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    nodes = {"-1": Channel.subscribe_to("input") | add_one | Channel.write_to("-1")}
    for i in range(test_size - 2):
        nodes[str(i)] = (
            Channel.subscribe_to(str(i - 1)) | add_one | Channel.write_to(str(i))
        )
    nodes["last"] = Channel.subscribe_to(str(i)) | add_one | Channel.write_to("output")

    app = Pregel(
        nodes=nodes,
        channels={str(i): LastValue(int) for i in range(-1, test_size - 2)}
        | {"input": LastValue(int), "output": LastValue(int)},
        input_channels="input",
        output_channels="output",
    )

    for _ in range(10):
        assert app.invoke(2, {"recursion_limit": test_size}) == 2 + test_size

    with ThreadPoolExecutor() as executor:
        assert [
            *executor.map(app.invoke, [2] * 10, [{"recursion_limit": test_size}] * 10)
        ] == [2 + test_size] * 10


def test_batch_many_processes_in_out(mocker: MockerFixture) -> None:
    test_size = 100
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    nodes = {"-1": Channel.subscribe_to("input") | add_one | Channel.write_to("-1")}
    for i in range(test_size - 2):
        nodes[str(i)] = (
            Channel.subscribe_to(str(i - 1)) | add_one | Channel.write_to(str(i))
        )
    nodes["last"] = Channel.subscribe_to(str(i)) | add_one | Channel.write_to("output")

    app = Pregel(
        nodes=nodes,
        channels={str(i): LastValue(int) for i in range(-1, test_size - 2)}
        | {"input": LastValue(int), "output": LastValue(int)},
        input_channels="input",
        output_channels="output",
    )

    for _ in range(3):
        assert app.batch([2, 1, 3, 4, 5], {"recursion_limit": test_size}) == [
            2 + test_size,
            1 + test_size,
            3 + test_size,
            4 + test_size,
            5 + test_size,
        ]

    with ThreadPoolExecutor() as executor:
        assert [
            *executor.map(
                app.batch, [[2, 1, 3, 4, 5]] * 3, [{"recursion_limit": test_size}] * 3
            )
        ] == [
            [2 + test_size, 1 + test_size, 3 + test_size, 4 + test_size, 5 + test_size]
        ] * 3


def test_invoke_two_processes_two_in_two_out_invalid(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    one = Channel.subscribe_to("input") | add_one | Channel.write_to("output")
    two = Channel.subscribe_to("input") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={"output": LastValue(int), "input": LastValue(int)},
        input_channels="input",
        output_channels="output",
    )

    with pytest.raises(InvalidUpdateError):
        # LastValue channels can only be updated once per iteration
        app.invoke(2)

    class State(TypedDict):
        hello: str

    def my_node(input: State) -> State:
        return {"hello": "world"}

    builder = StateGraph(State)
    builder.add_node("one", my_node)
    builder.add_node("two", my_node)
    builder.set_conditional_entry_point(lambda _: ["one", "two"])

    graph = builder.compile()
    with pytest.raises(InvalidUpdateError, match="At key 'hello'"):
        graph.invoke({"hello": "there"}, debug=True)


def test_invoke_two_processes_two_in_two_out_valid(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    one = Channel.subscribe_to("input") | add_one | Channel.write_to("output")
    two = Channel.subscribe_to("input") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "input": LastValue(int),
            "output": Topic(int),
        },
        input_channels="input",
        output_channels="output",
    )

    # An Inbox channel accumulates updates into a sequence
    assert app.invoke(2) == [3, 3]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_invoke_checkpoint_two(
    mocker: MockerFixture, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )
    add_one = mocker.Mock(side_effect=lambda x: x["total"] + x["input"])
    errored_once = False

    def raise_if_above_10(input: int) -> int:
        nonlocal errored_once
        if input > 4:
            if errored_once:
                pass
            else:
                errored_once = True
                raise ConnectionError("I will be retried")
        if input > 10:
            raise ValueError("Input is too large")
        return input

    one = (
        Channel.subscribe_to(["input"]).join(["total"])
        | add_one
        | Channel.write_to("output", "total")
        | raise_if_above_10
    )

    app = Pregel(
        nodes={"one": one},
        channels={
            "total": BinaryOperatorAggregate(int, operator.add),
            "input": LastValue(int),
            "output": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
        checkpointer=checkpointer,
        retry_policy=RetryPolicy(),
    )

    # total starts out as 0, so output is 0+2=2
    assert app.invoke(2, {"configurable": {"thread_id": "1"}}) == 2
    checkpoint = checkpointer.get({"configurable": {"thread_id": "1"}})
    assert checkpoint is not None
    assert checkpoint["channel_values"].get("total") == 2
    # total is now 2, so output is 2+3=5
    assert app.invoke(3, {"configurable": {"thread_id": "1"}}) == 5
    assert errored_once, "errored and retried"
    checkpoint_tup = checkpointer.get_tuple({"configurable": {"thread_id": "1"}})
    assert checkpoint_tup is not None
    assert checkpoint_tup.checkpoint["channel_values"].get("total") == 7
    # total is now 2+5=7, so output would be 7+4=11, but raises ValueError
    with pytest.raises(ValueError):
        app.invoke(4, {"configurable": {"thread_id": "1"}})
    # checkpoint is not updated, error is recorded
    checkpoint_tup = checkpointer.get_tuple({"configurable": {"thread_id": "1"}})
    assert checkpoint_tup is not None
    assert checkpoint_tup.checkpoint["channel_values"].get("total") == 7
    assert checkpoint_tup.pending_writes == [
        (AnyStr(), ERROR, "ValueError('Input is too large')")
    ]
    # on a new thread, total starts out as 0, so output is 0+5=5
    assert app.invoke(5, {"configurable": {"thread_id": "2"}}) == 5
    checkpoint = checkpointer.get({"configurable": {"thread_id": "1"}})
    assert checkpoint is not None
    assert checkpoint["channel_values"].get("total") == 7
    checkpoint = checkpointer.get({"configurable": {"thread_id": "2"}})
    assert checkpoint is not None
    assert checkpoint["channel_values"].get("total") == 5


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_pending_writes_resume(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    class State(TypedDict):
        value: Annotated[int, operator.add]

    class AwhileMaker:
        def __init__(self, sleep: float, rtn: Union[Dict, Exception]) -> None:
            self.sleep = sleep
            self.rtn = rtn
            self.reset()

        def __call__(self, input: State) -> Any:
            self.calls += 1
            time.sleep(self.sleep)
            if isinstance(self.rtn, Exception):
                raise self.rtn
            else:
                return self.rtn

        def reset(self):
            self.calls = 0

    one = AwhileMaker(0.1, {"value": 2})
    two = AwhileMaker(0.3, ConnectionError("I'm not good"))
    builder = StateGraph(State)
    builder.add_node("one", one)
    builder.add_node("two", two, retry=RetryPolicy(max_attempts=2))
    builder.add_edge(START, "one")
    builder.add_edge(START, "two")
    graph = builder.compile(checkpointer=checkpointer)

    thread1: RunnableConfig = {"configurable": {"thread_id": "1"}}
    with pytest.raises(ConnectionError, match="I'm not good"):
        graph.invoke({"value": 1}, thread1)

    # both nodes should have been called once
    assert one.calls == 1
    assert two.calls == 2  # two attempts

    # latest checkpoint should be before nodes "one", "two"
    state = graph.get_state(thread1)
    assert state is not None
    assert state.values == {"value": 1}
    assert state.next == ("one", "two")
    assert state.tasks == (
        PregelTask(AnyStr(), "one", (PULL, "one"), result={"value": 2}),
        PregelTask(AnyStr(), "two", (PULL, "two"), 'ConnectionError("I\'m not good")'),
    )
    assert state.metadata == {
        "parents": {},
        "source": "loop",
        "step": 0,
        "writes": None,
    }
    # should contain pending write of "one"
    checkpoint = checkpointer.get_tuple(thread1)
    assert checkpoint is not None
    # should contain error from "two"
    expected_writes = [
        (AnyStr(), "one", "one"),
        (AnyStr(), "value", 2),
        (AnyStr(), ERROR, 'ConnectionError("I\'m not good")'),
    ]
    assert len(checkpoint.pending_writes) == 3
    assert all(w in expected_writes for w in checkpoint.pending_writes)
    # both non-error pending writes come from same task
    non_error_writes = [w for w in checkpoint.pending_writes if w[1] != ERROR]
    assert non_error_writes[0][0] == non_error_writes[1][0]
    # error write is from the other task
    error_write = next(w for w in checkpoint.pending_writes if w[1] == ERROR)
    assert error_write[0] != non_error_writes[0][0]

    # resume execution
    with pytest.raises(ConnectionError, match="I'm not good"):
        graph.invoke(None, thread1)

    # node "one" succeeded previously, so shouldn't be called again
    assert one.calls == 1
    # node "two" should have been called once again
    assert two.calls == 4  # two attempts before + two attempts now

    # confirm no new checkpoints saved
    state_two = graph.get_state(thread1)
    assert state_two.metadata == state.metadata

    # resume execution, without exception
    two.rtn = {"value": 3}
    # both the pending write and the new write were applied, 1 + 2 + 3 = 6
    assert graph.invoke(None, thread1) == {"value": 6}

    # check all final checkpoints
    checkpoints = [c for c in checkpointer.list(thread1)]
    # we should have 3
    assert len(checkpoints) == 3
    # the last one not too interesting for this test
    assert checkpoints[0] == CheckpointTuple(
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        checkpoint={
            "v": 1,
            "id": AnyStr(),
            "ts": AnyStr(),
            "pending_sends": [],
            "versions_seen": {
                "one": {
                    "start:one": AnyVersion(),
                },
                "two": {
                    "start:two": AnyVersion(),
                },
                "__input__": {},
                "__start__": {
                    "__start__": AnyVersion(),
                },
                "__interrupt__": {
                    "value": AnyVersion(),
                    "__start__": AnyVersion(),
                    "start:one": AnyVersion(),
                    "start:two": AnyVersion(),
                },
            },
            "channel_versions": {
                "one": AnyVersion(),
                "two": AnyVersion(),
                "value": AnyVersion(),
                "__start__": AnyVersion(),
                "start:one": AnyVersion(),
                "start:two": AnyVersion(),
            },
            "channel_values": {"one": "one", "two": "two", "value": 6},
        },
        metadata={
            "parents": {},
            "step": 1,
            "source": "loop",
            "writes": {"one": {"value": 2}, "two": {"value": 3}},
        },
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoints[1].config["configurable"]["checkpoint_id"],
            }
        },
        pending_writes=[],
    )
    # the previous one we assert that pending writes contains both
    # - original error
    # - successful writes from resuming after preventing error
    assert checkpoints[1] == CheckpointTuple(
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        checkpoint={
            "v": 1,
            "id": AnyStr(),
            "ts": AnyStr(),
            "pending_sends": [],
            "versions_seen": {
                "__input__": {},
                "__start__": {
                    "__start__": AnyVersion(),
                },
            },
            "channel_versions": {
                "value": AnyVersion(),
                "__start__": AnyVersion(),
                "start:one": AnyVersion(),
                "start:two": AnyVersion(),
            },
            "channel_values": {
                "value": 1,
                "start:one": "__start__",
                "start:two": "__start__",
            },
        },
        metadata={"parents": {}, "step": 0, "source": "loop", "writes": None},
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoints[2].config["configurable"]["checkpoint_id"],
            }
        },
        pending_writes=UnsortedSequence(
            (AnyStr(), "one", "one"),
            (AnyStr(), "value", 2),
            (AnyStr(), "__error__", 'ConnectionError("I\'m not good")'),
            (AnyStr(), "two", "two"),
            (AnyStr(), "value", 3),
        ),
    )
    assert checkpoints[2] == CheckpointTuple(
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        checkpoint={
            "v": 1,
            "id": AnyStr(),
            "ts": AnyStr(),
            "pending_sends": [],
            "versions_seen": {"__input__": {}},
            "channel_versions": {
                "__start__": AnyVersion(),
            },
            "channel_values": {"__start__": {"value": 1}},
        },
        metadata={
            "parents": {},
            "step": -1,
            "source": "input",
            "writes": {"__start__": {"value": 1}},
        },
        parent_config=None,
        pending_writes=UnsortedSequence(
            (AnyStr(), "value", 1),
            (AnyStr(), "start:one", "__start__"),
            (AnyStr(), "start:two", "__start__"),
        ),
    )


def test_cond_edge_after_send() -> None:
    class Node:
        def __init__(self, name: str):
            self.name = name
            setattr(self, "__name__", name)

        def __call__(self, state):
            return [self.name]

    def send_for_fun(state):
        return [Send("2", state), Send("2", state)]

    def route_to_three(state) -> Literal["3"]:
        return "3"

    builder = StateGraph(Annotated[list, operator.add])
    builder.add_node(Node("1"))
    builder.add_node(Node("2"))
    builder.add_node(Node("3"))
    builder.add_edge(START, "1")
    builder.add_conditional_edges("1", send_for_fun)
    builder.add_conditional_edges("2", route_to_three)
    graph = builder.compile()
    assert graph.invoke(["0"]) == ["0", "1", "2", "2", "3"]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_invoke_checkpoint_three(
    mocker: MockerFixture, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    adder = mocker.Mock(side_effect=lambda x: x["total"] + x["input"])

    def raise_if_above_10(input: int) -> int:
        if input > 10:
            raise ValueError("Input is too large")
        return input

    one = (
        Channel.subscribe_to(["input"]).join(["total"])
        | adder
        | Channel.write_to("output", "total")
        | raise_if_above_10
    )

    app = Pregel(
        nodes={"one": one},
        channels={
            "total": BinaryOperatorAggregate(int, operator.add),
            "input": LastValue(int),
            "output": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
        checkpointer=checkpointer,
    )

    thread_1 = {"configurable": {"thread_id": "1"}}
    # total starts out as 0, so output is 0+2=2
    assert app.invoke(2, thread_1, debug=1) == 2
    state = app.get_state(thread_1)
    assert state is not None
    assert state.values.get("total") == 2
    assert state.next == ()
    assert (
        state.config["configurable"]["checkpoint_id"]
        == checkpointer.get(thread_1)["id"]
    )
    # total is now 2, so output is 2+3=5
    assert app.invoke(3, thread_1) == 5
    state = app.get_state(thread_1)
    assert state is not None
    assert state.values.get("total") == 7
    assert (
        state.config["configurable"]["checkpoint_id"]
        == checkpointer.get(thread_1)["id"]
    )
    # total is now 2+5=7, so output would be 7+4=11, but raises ValueError
    with pytest.raises(ValueError):
        app.invoke(4, thread_1)
    # checkpoint is updated with new input
    state = app.get_state(thread_1)
    assert state is not None
    assert state.values.get("total") == 7
    assert state.next == ("one",)
    """we checkpoint inputs and it failed on "one", so the next node is one"""
    # we can recover from error by sending new inputs
    assert app.invoke(2, thread_1) == 9
    state = app.get_state(thread_1)
    assert state is not None
    assert state.values.get("total") == 16, "total is now 7+9=16"
    assert state.next == ()

    thread_2 = {"configurable": {"thread_id": "2"}}
    # on a new thread, total starts out as 0, so output is 0+5=5
    assert app.invoke(5, thread_2, debug=True) == 5
    state = app.get_state({"configurable": {"thread_id": "1"}})
    assert state is not None
    assert state.values.get("total") == 16
    assert state.next == (), "checkpoint of other thread not touched"
    state = app.get_state(thread_2)
    assert state is not None
    assert state.values.get("total") == 5
    assert state.next == ()

    assert len(list(app.get_state_history(thread_1, limit=1))) == 1
    # list all checkpoints for thread 1
    thread_1_history = [c for c in app.get_state_history(thread_1)]
    # there are 7 checkpoints
    assert len(thread_1_history) == 7
    assert Counter(c.metadata["source"] for c in thread_1_history) == {
        "input": 4,
        "loop": 3,
    }
    # sorted descending
    assert (
        thread_1_history[0].config["configurable"]["checkpoint_id"]
        > thread_1_history[1].config["configurable"]["checkpoint_id"]
    )
    # cursor pagination
    cursored = list(
        app.get_state_history(thread_1, limit=1, before=thread_1_history[0].config)
    )
    assert len(cursored) == 1
    assert cursored[0].config == thread_1_history[1].config
    # the last checkpoint
    assert thread_1_history[0].values["total"] == 16
    # the first "loop" checkpoint
    assert thread_1_history[-2].values["total"] == 2
    # can get each checkpoint using aget with config
    assert (
        checkpointer.get(thread_1_history[0].config)["id"]
        == thread_1_history[0].config["configurable"]["checkpoint_id"]
    )
    assert (
        checkpointer.get(thread_1_history[1].config)["id"]
        == thread_1_history[1].config["configurable"]["checkpoint_id"]
    )

    thread_1_next_config = app.update_state(thread_1_history[1].config, 10)
    # update creates a new checkpoint
    assert (
        thread_1_next_config["configurable"]["checkpoint_id"]
        > thread_1_history[0].config["configurable"]["checkpoint_id"]
    )
    # update makes new checkpoint child of the previous one
    assert (
        app.get_state(thread_1_next_config).parent_config == thread_1_history[1].config
    )
    # 1 more checkpoint in history
    assert len(list(app.get_state_history(thread_1))) == 8
    assert Counter(c.metadata["source"] for c in app.get_state_history(thread_1)) == {
        "update": 1,
        "input": 4,
        "loop": 3,
    }
    # the latest checkpoint is the updated one
    assert app.get_state(thread_1) == app.get_state(thread_1_next_config)


def test_invoke_two_processes_two_in_join_two_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    add_10_each = mocker.Mock(side_effect=lambda x: sorted(y + 10 for y in x))

    one = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    chain_three = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    chain_four = (
        Channel.subscribe_to("inbox") | add_10_each | Channel.write_to("output")
    )

    app = Pregel(
        nodes={
            "one": one,
            "chain_three": chain_three,
            "chain_four": chain_four,
        },
        channels={
            "inbox": Topic(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    # Then invoke app
    # We get a single array result as chain_four waits for all publishers to finish
    # before operating on all elements published to topic_two as an array
    for _ in range(100):
        assert app.invoke(2) == [13, 13]

    with ThreadPoolExecutor() as executor:
        assert [*executor.map(app.invoke, [2] * 100)] == [[13, 13]] * 100


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_invoke_join_then_call_other_pregel(
    mocker: MockerFixture, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")

    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    add_10_each = mocker.Mock(side_effect=lambda x: [y + 10 for y in x])

    inner_app = Pregel(
        nodes={
            "one": Channel.subscribe_to("input") | add_one | Channel.write_to("output")
        },
        channels={
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    one = (
        Channel.subscribe_to("input")
        | add_10_each
        | Channel.write_to("inbox_one").map()
    )
    two = (
        Channel.subscribe_to("inbox_one")
        | inner_app.map()
        | sorted
        | Channel.write_to("outbox_one")
    )
    chain_three = Channel.subscribe_to("outbox_one") | sum | Channel.write_to("output")

    app = Pregel(
        nodes={
            "one": one,
            "two": two,
            "chain_three": chain_three,
        },
        channels={
            "inbox_one": Topic(int),
            "outbox_one": LastValue(int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    for _ in range(10):
        assert app.invoke([2, 3]) == 27

    with ThreadPoolExecutor() as executor:
        assert [*executor.map(app.invoke, [[2, 3]] * 10)] == [27] * 10

    # add checkpointer
    app.checkpointer = checkpointer
    # subgraph is called twice in the same node, through .map(), so raises
    with pytest.raises(MultipleSubgraphsError):
        app.invoke([2, 3], {"configurable": {"thread_id": "1"}})

    # set inner graph checkpointer NeverCheckpoint
    inner_app.checkpointer = False
    # subgraph still called twice, but checkpointing for inner graph is disabled
    assert app.invoke([2, 3], {"configurable": {"thread_id": "1"}}) == 27


def test_invoke_two_processes_one_in_two_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    one = (
        Channel.subscribe_to("input") | add_one | Channel.write_to("output", "between")
    )
    two = Channel.subscribe_to("between") | add_one | Channel.write_to("output")

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "input": LastValue(int),
            "between": LastValue(int),
            "output": LastValue(int),
        },
        stream_channels=["output", "between"],
        input_channels="input",
        output_channels="output",
    )

    assert [c for c in app.stream(2, stream_mode="updates")] == [
        {"one": {"between": 3, "output": 3}},
        {"two": {"output": 4}},
    ]
    assert [c for c in app.stream(2)] == [
        {"between": 3, "output": 3},
        {"between": 3, "output": 4},
    ]


def test_invoke_two_processes_no_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    one = Channel.subscribe_to("input") | add_one | Channel.write_to("between")
    two = Channel.subscribe_to("between") | add_one

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "input": LastValue(int),
            "between": LastValue(int),
            "output": LastValue(int),
        },
        input_channels="input",
        output_channels="output",
    )

    # It finishes executing (once no more messages being published)
    # but returns nothing, as nothing was published to OUT topic
    assert app.invoke(2) is None


def test_invoke_two_processes_no_in(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    one = Channel.subscribe_to("between") | add_one | Channel.write_to("output")
    two = Channel.subscribe_to("between") | add_one

    with pytest.raises(TypeError):
        Pregel(nodes={"one": one, "two": two})


def test_channel_enter_exit_timing(mocker: MockerFixture) -> None:
    setup = mocker.Mock()
    cleanup = mocker.Mock()

    @contextmanager
    def an_int() -> Generator[int, None, None]:
        setup()
        try:
            yield 5
        finally:
            cleanup()

    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    one = Channel.subscribe_to("input") | add_one | Channel.write_to("inbox")
    two = (
        Channel.subscribe_to("inbox")
        | RunnableLambda(add_one).batch
        | Channel.write_to("output").batch
    )

    app = Pregel(
        nodes={"one": one, "two": two},
        channels={
            "inbox": Topic(int),
            "ctx": Context(an_int),
            "output": LastValue(int),
            "input": LastValue(int),
        },
        input_channels="input",
        output_channels=["inbox", "output"],
        stream_channels=["inbox", "output"],
    )

    assert setup.call_count == 0
    assert cleanup.call_count == 0
    for i, chunk in enumerate(app.stream(2)):
        assert setup.call_count == 1, "Expected setup to be called once"
        if i == 0:
            assert chunk == {"inbox": [3]}
        elif i == 1:
            assert chunk == {"output": 4}
        else:
            assert False, "Expected only two chunks"
    assert cleanup.call_count == 1, "Expected cleanup to be called once"


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_conditional_graph(
    snapshot: SnapshotAssertion, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    from langchain_core.language_models.fake import FakeStreamingListLLM
    from langchain_core.prompts import PromptTemplate
    from langchain_core.runnables import RunnablePassthrough
    from langchain_core.tools import tool

    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    # Assemble the tools
    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    # Construct the agent
    prompt = PromptTemplate.from_template("Hello!")

    llm = FakeStreamingListLLM(
        responses=[
            "tool:search_api:query",
            "tool:search_api:another",
            "finish:answer",
        ]
    )

    def agent_parser(input: str) -> Union[AgentAction, AgentFinish]:
        if input.startswith("finish"):
            _, answer = input.split(":")
            return AgentFinish(return_values={"answer": answer}, log=input)
        else:
            _, tool_name, tool_input = input.split(":")
            return AgentAction(tool=tool_name, tool_input=tool_input, log=input)

    agent = RunnablePassthrough.assign(agent_outcome=prompt | llm | agent_parser)

    # Define tool execution logic
    def execute_tools(data: dict) -> dict:
        data = data.copy()
        agent_action: AgentAction = data.pop("agent_outcome")
        observation = {t.name: t for t in tools}[agent_action.tool].invoke(
            agent_action.tool_input
        )
        if data.get("intermediate_steps") is None:
            data["intermediate_steps"] = []
        else:
            data["intermediate_steps"] = data["intermediate_steps"].copy()
        data["intermediate_steps"].append([agent_action, observation])
        return data

    # Define decision-making logic
    def should_continue(data: dict) -> str:
        # Logic to decide whether to continue in the loop or exit
        if isinstance(data["agent_outcome"], AgentFinish):
            return "exit"
        else:
            return "continue"

    # Define a new graph
    workflow = Graph()

    workflow.add_node("agent", agent)
    workflow.add_node(
        "tools", execute_tools, metadata={"parents": {}, "version": 2, "variant": "b"}
    )

    workflow.set_entry_point("agent")

    workflow.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "exit": END}
    )

    workflow.add_edge("tools", "agent")

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot
        assert app.get_graph().draw_mermaid() == snapshot
        assert json.dumps(app.get_graph(xray=True).to_json(), indent=2) == snapshot
        assert app.get_graph(xray=True).draw_mermaid(with_styles=False) == snapshot

    assert app.invoke({"input": "what is weather in sf"}) == {
        "input": "what is weather in sf",
        "intermediate_steps": [
            [
                AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:query",
                ),
                "result for query",
            ],
            [
                AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
                "result for another",
            ],
        ],
        "agent_outcome": AgentFinish(
            return_values={"answer": "answer"}, log="finish:answer"
        ),
    }

    assert [c for c in app.stream({"input": "what is weather in sf"})] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ],
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="another",
                            log="tool:search_api:another",
                        ),
                        "result for another",
                    ],
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ],
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="another",
                            log="tool:search_api:another",
                        ),
                        "result for another",
                    ],
                ],
                "agent_outcome": AgentFinish(
                    return_values={"answer": "answer"}, log="finish:answer"
                ),
            }
        },
    ]

    # test state get/update methods with interrupt_after

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["agent"],
    )
    config = {"configurable": {"thread_id": "1"}}

    if SHOULD_CHECK_SNAPSHOTS:
        assert app_w_interrupt.get_graph().to_json() == snapshot
        assert app_w_interrupt.get_graph().draw_mermaid() == snapshot

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        }
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            },
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        metadata={
            "parents": {},
            "source": "loop",
            "step": 0,
            "writes": {
                "agent": {
                    "agent": {
                        "input": "what is weather in sf",
                        "agent_outcome": AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                    }
                },
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )
    assert (
        app_w_interrupt.checkpointer.get_tuple(config).config["configurable"][
            "checkpoint_id"
        ]
        is not None
    )

    app_w_interrupt.update_state(
        config,
        {
            "agent_outcome": AgentAction(
                tool="search_api",
                tool_input="query",
                log="tool:search_api:a different query",
            ),
            "input": "what is weather in sf",
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:a different query",
                ),
                "input": "what is weather in sf",
            },
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "input": "what is weather in sf",
                },
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:a different query",
                ),
                "input": "what is weather in sf",
            }
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
    ]

    app_w_interrupt.update_state(
        config,
        {
            "input": "what is weather in sf",
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "result for query",
                ]
            ],
            "agent_outcome": AgentFinish(
                return_values={"answer": "a really nice answer"},
                log="finish:a really nice answer",
            ),
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentFinish(
                    return_values={"answer": "a really nice answer"},
                    log="finish:a really nice answer",
                ),
            },
        },
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 4,
            "writes": {
                "agent": {
                    "input": "what is weather in sf",
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:a different query",
                            ),
                            "result for query",
                        ]
                    ],
                    "agent_outcome": AgentFinish(
                        return_values={"answer": "a really nice answer"},
                        log="finish:a really nice answer",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # test state get/update methods with interrupt_before

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["tools"],
    )
    config = {"configurable": {"thread_id": "2"}}
    llm.i = 0  # reset the llm

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        }
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            },
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 0,
            "writes": {
                "agent": {
                    "agent": {
                        "input": "what is weather in sf",
                        "agent_outcome": AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                    }
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        {
            "agent_outcome": AgentAction(
                tool="search_api",
                tool_input="query",
                log="tool:search_api:a different query",
            ),
            "input": "what is weather in sf",
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:a different query",
                ),
                "input": "what is weather in sf",
            },
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "input": "what is weather in sf",
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:a different query",
                ),
                "input": "what is weather in sf",
            },
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
    ]

    app_w_interrupt.update_state(
        config,
        {
            "input": "what is weather in sf",
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "result for query",
                ]
            ],
            "agent_outcome": AgentFinish(
                return_values={"answer": "a really nice answer"},
                log="finish:a really nice answer",
            ),
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentFinish(
                    return_values={"answer": "a really nice answer"},
                    log="finish:a really nice answer",
                ),
            },
        },
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 4,
            "writes": {
                "agent": {
                    "input": "what is weather in sf",
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:a different query",
                            ),
                            "result for query",
                        ]
                    ],
                    "agent_outcome": AgentFinish(
                        return_values={"answer": "a really nice answer"},
                        log="finish:a really nice answer",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # test re-invoke to continue with interrupt_before

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["tools"],
    )
    config = {"configurable": {"thread_id": "3"}}
    llm.i = 0  # reset the llm

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        }
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            },
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 0,
            "writes": {
                "agent": {
                    "agent": {
                        "input": "what is weather in sf",
                        "agent_outcome": AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                    }
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            },
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
    ]

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
        {
            "tools": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ],
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="another",
                            log="tool:search_api:another",
                        ),
                        "result for another",
                    ],
                ],
            }
        },
        {
            "agent": {
                "input": "what is weather in sf",
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ],
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="another",
                            log="tool:search_api:another",
                        ),
                        "result for another",
                    ],
                ],
                "agent_outcome": AgentFinish(
                    return_values={"answer": "answer"}, log="finish:answer"
                ),
            }
        },
    ]


def test_conditional_entrypoint_graph(snapshot: SnapshotAssertion) -> None:
    def left(data: str) -> str:
        return data + "->left"

    def right(data: str) -> str:
        return data + "->right"

    def should_start(data: str) -> str:
        # Logic to decide where to start
        if len(data) > 10:
            return "go-right"
        else:
            return "go-left"

    # Define a new graph
    workflow = Graph()

    workflow.add_node("left", left)
    workflow.add_node("right", right)

    workflow.set_conditional_entry_point(
        should_start, {"go-left": "left", "go-right": "right"}
    )

    workflow.add_conditional_edges("left", lambda data: END, {END: END})
    workflow.add_edge("right", END)

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert (
        app.invoke("what is weather in sf", debug=True)
        == "what is weather in sf->right"
    )

    assert [*app.stream("what is weather in sf")] == [
        {"right": "what is weather in sf->right"},
    ]


def test_conditional_entrypoint_to_multiple_state_graph(
    snapshot: SnapshotAssertion,
) -> None:
    class OverallState(TypedDict):
        locations: list[str]
        results: Annotated[list[str], operator.add]

    def get_weather(state: OverallState) -> OverallState:
        location = state["location"]
        weather = "sunny" if len(location) > 2 else "cloudy"
        return {"results": [f"It's {weather} in {location}"]}

    def continue_to_weather(state: OverallState) -> list[Send]:
        return [
            Send("get_weather", {"location": location})
            for location in state["locations"]
        ]

    workflow = StateGraph(OverallState)

    workflow.add_node("get_weather", get_weather)
    workflow.add_edge("get_weather", END)
    workflow.set_conditional_entry_point(continue_to_weather)

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke({"locations": ["sf", "nyc"]}, debug=True) == {
        "locations": ["sf", "nyc"],
        "results": ["It's cloudy in sf", "It's sunny in nyc"],
    }

    assert [*app.stream({"locations": ["sf", "nyc"]}, stream_mode="values")][-1] == {
        "locations": ["sf", "nyc"],
        "results": ["It's cloudy in sf", "It's sunny in nyc"],
    }


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_conditional_state_graph(
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    request: pytest.FixtureRequest,
    checkpointer_name: str,
) -> None:
    from langchain_core.language_models.fake import FakeStreamingListLLM
    from langchain_core.prompts import PromptTemplate
    from langchain_core.tools import tool

    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )
    setup = mocker.Mock()
    teardown = mocker.Mock()

    @contextmanager
    def assert_ctx_once() -> Iterator[None]:
        assert setup.call_count == 0
        assert teardown.call_count == 0
        try:
            yield
        finally:
            assert setup.call_count == 1
            assert teardown.call_count == 1
            setup.reset_mock()
            teardown.reset_mock()

    @contextmanager
    def make_httpx_client() -> Iterator[httpx.Client]:
        setup()
        with httpx.Client() as client:
            try:
                yield client
            finally:
                teardown()

    class AgentState(TypedDict, total=False):
        input: Annotated[str, UntrackedValue]
        agent_outcome: Optional[Union[AgentAction, AgentFinish]]
        intermediate_steps: Annotated[list[tuple[AgentAction, str]], operator.add]
        session: Annotated[httpx.Client, Context(make_httpx_client)]

    class ToolState(TypedDict, total=False):
        agent_outcome: Union[AgentAction, AgentFinish]
        session: Annotated[httpx.Client, Context(make_httpx_client)]

    # Assemble the tools
    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    # Construct the agent
    prompt = PromptTemplate.from_template("Hello!")

    llm = FakeStreamingListLLM(
        responses=[
            "tool:search_api:query",
            "tool:search_api:another",
            "finish:answer",
        ]
    )

    def agent_parser(input: str) -> dict[str, Union[AgentAction, AgentFinish]]:
        if input.startswith("finish"):
            _, answer = input.split(":")
            return {
                "agent_outcome": AgentFinish(
                    return_values={"answer": answer}, log=input
                )
            }
        else:
            _, tool_name, tool_input = input.split(":")
            return {
                "agent_outcome": AgentAction(
                    tool=tool_name, tool_input=tool_input, log=input
                )
            }

    agent = prompt | llm | agent_parser

    # Define tool execution logic
    def execute_tools(data: ToolState) -> dict:
        # check session in data
        assert isinstance(data["session"], httpx.Client)
        assert "input" not in data
        assert "intermediate_steps" not in data
        # execute the tool
        agent_action: AgentAction = data.pop("agent_outcome")
        observation = {t.name: t for t in tools}[agent_action.tool].invoke(
            agent_action.tool_input
        )
        return {"intermediate_steps": [[agent_action, observation]]}

    # Define decision-making logic
    def should_continue(data: AgentState) -> str:
        # check session in data
        assert isinstance(data["session"], httpx.Client)
        # Logic to decide whether to continue in the loop or exit
        if isinstance(data["agent_outcome"], AgentFinish):
            return "exit"
        else:
            return "continue"

    # Define a new graph
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", agent)
    workflow.add_node("tools", execute_tools, input=ToolState)

    workflow.set_entry_point("agent")

    workflow.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "exit": END}
    )

    workflow.add_edge("tools", "agent")

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    with assert_ctx_once():
        assert app.invoke({"input": "what is weather in sf"}) == {
            "input": "what is weather in sf",
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                    "result for query",
                ],
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="another",
                        log="tool:search_api:another",
                    ),
                    "result for another",
                ],
            ],
            "agent_outcome": AgentFinish(
                return_values={"answer": "answer"}, log="finish:answer"
            ),
        }

    with assert_ctx_once():
        assert [*app.stream({"input": "what is weather in sf"})] == [
            {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
            {
                "tools": {
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:query",
                            ),
                            "result for query",
                        ]
                    ],
                }
            },
            {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="another",
                        log="tool:search_api:another",
                    ),
                }
            },
            {
                "tools": {
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="another",
                                log="tool:search_api:another",
                            ),
                            "result for another",
                        ],
                    ],
                }
            },
            {
                "agent": {
                    "agent_outcome": AgentFinish(
                        return_values={"answer": "answer"}, log="finish:answer"
                    ),
                }
            },
        ]

    # test state get/update methods with interrupt_after

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["agent"],
    )
    config = {"configurable": {"thread_id": "1"}}

    with assert_ctx_once():
        assert [
            c
            for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
        ] == [
            {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
            {"__interrupt__": ()},
        ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    with assert_ctx_once():
        app_w_interrupt.update_state(
            config,
            {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="query",
                    log="tool:search_api:a different query",
                )
            },
        )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api",
                tool_input="query",
                log="tool:search_api:a different query",
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    )
                },
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    with assert_ctx_once():
        assert [c for c in app_w_interrupt.stream(None, config)] == [
            {
                "tools": {
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:a different query",
                            ),
                            "result for query",
                        ]
                    ],
                }
            },
            {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="another",
                        log="tool:search_api:another",
                    ),
                }
            },
            {"__interrupt__": ()},
        ]

    with assert_ctx_once():
        app_w_interrupt.update_state(
            config,
            {
                "agent_outcome": AgentFinish(
                    return_values={"answer": "a really nice answer"},
                    log="finish:a really nice answer",
                )
            },
        )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentFinish(
                return_values={"answer": "a really nice answer"},
                log="finish:a really nice answer",
            ),
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "result for query",
                ]
            ],
        },
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {
                "agent": {
                    "agent_outcome": AgentFinish(
                        return_values={"answer": "a really nice answer"},
                        log="finish:a really nice answer",
                    )
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # test state get/update methods with interrupt_before

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["tools"],
        debug=True,
    )
    config = {"configurable": {"thread_id": "2"}}
    llm.i = 0  # reset the llm

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        {
            "agent_outcome": AgentAction(
                tool="search_api",
                tool_input="query",
                log="tool:search_api:a different query",
            )
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api",
                tool_input="query",
                log="tool:search_api:a different query",
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    )
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": {
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:a different query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
        {"__interrupt__": ()},
    ]

    app_w_interrupt.update_state(
        config,
        {
            "agent_outcome": AgentFinish(
                return_values={"answer": "a really nice answer"},
                log="finish:a really nice answer",
            )
        },
    )

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentFinish(
                return_values={"answer": "a really nice answer"},
                log="finish:a really nice answer",
            ),
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:a different query",
                    ),
                    "result for query",
                ]
            ],
        },
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {
                "agent": {
                    "agent_outcome": AgentFinish(
                        return_values={"answer": "a really nice answer"},
                        log="finish:a really nice answer",
                    )
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # test w interrupt before all
    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before="*",
        debug=True,
    )
    config = {"configurable": {"thread_id": "3"}}
    llm.i = 0  # reset the llm

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": {
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                    "result for query",
                ]
            ],
        },
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 2,
            "writes": {
                "tools": {
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:query",
                            ),
                            "result for query",
                        ]
                    ],
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
        {"__interrupt__": ()},
    ]

    # test w interrupt after all
    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after="*",
    )
    config = {"configurable": {"thread_id": "4"}}
    llm.i = 0  # reset the llm

    assert [
        c for c in app_w_interrupt.stream({"input": "what is weather in sf"}, config)
    ] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [],
        },
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": {
                    "agent_outcome": AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": {
                "intermediate_steps": [
                    [
                        AgentAction(
                            tool="search_api",
                            tool_input="query",
                            log="tool:search_api:query",
                        ),
                        "result for query",
                    ]
                ],
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "agent_outcome": AgentAction(
                tool="search_api", tool_input="query", log="tool:search_api:query"
            ),
            "intermediate_steps": [
                [
                    AgentAction(
                        tool="search_api",
                        tool_input="query",
                        log="tool:search_api:query",
                    ),
                    "result for query",
                ]
            ],
        },
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 2,
            "writes": {
                "tools": {
                    "intermediate_steps": [
                        [
                            AgentAction(
                                tool="search_api",
                                tool_input="query",
                                log="tool:search_api:query",
                            ),
                            "result for query",
                        ]
                    ],
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "agent": {
                "agent_outcome": AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
            }
        },
        {"__interrupt__": ()},
    ]


def test_conditional_state_graph_with_list_edge_inputs(snapshot: SnapshotAssertion):
    class State(TypedDict):
        foo: Annotated[list[str], operator.add]

    graph_builder = StateGraph(State)
    graph_builder.add_node("A", lambda x: {"foo": ["A"]})
    graph_builder.add_node("B", lambda x: {"foo": ["B"]})
    graph_builder.add_edge(START, "A")
    graph_builder.add_edge(START, "B")
    graph_builder.add_edge(["A", "B"], END)

    app = graph_builder.compile()
    assert app.invoke({"foo": []}) == {"foo": ["A", "B"]}

    assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot


def test_state_graph_w_config_inherited_state_keys(snapshot: SnapshotAssertion) -> None:
    from langchain_core.language_models.fake import FakeStreamingListLLM
    from langchain_core.prompts import PromptTemplate
    from langchain_core.tools import tool

    class BaseState(TypedDict):
        input: str
        agent_outcome: Optional[Union[AgentAction, AgentFinish]]

    class AgentState(BaseState, total=False):
        intermediate_steps: Annotated[list[tuple[AgentAction, str]], operator.add]

    assert get_type_hints(AgentState).keys() == {
        "input",
        "agent_outcome",
        "intermediate_steps",
    }

    class Config(TypedDict, total=False):
        tools: list[str]

    # Assemble the tools
    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    # Construct the agent
    prompt = PromptTemplate.from_template("Hello!")

    llm = FakeStreamingListLLM(
        responses=[
            "tool:search_api:query",
            "tool:search_api:another",
            "finish:answer",
        ]
    )

    def agent_parser(input: str) -> dict[str, Union[AgentAction, AgentFinish]]:
        if input.startswith("finish"):
            _, answer = input.split(":")
            return {
                "agent_outcome": AgentFinish(
                    return_values={"answer": answer}, log=input
                )
            }
        else:
            _, tool_name, tool_input = input.split(":")
            return {
                "agent_outcome": AgentAction(
                    tool=tool_name, tool_input=tool_input, log=input
                )
            }

    agent = prompt | llm | agent_parser

    # Define tool execution logic
    def execute_tools(data: AgentState) -> dict:
        agent_action: AgentAction = data.pop("agent_outcome")
        observation = {t.name: t for t in tools}[agent_action.tool].invoke(
            agent_action.tool_input
        )
        return {"intermediate_steps": [(agent_action, observation)]}

    # Define decision-making logic
    def should_continue(data: AgentState) -> str:
        # Logic to decide whether to continue in the loop or exit
        if isinstance(data["agent_outcome"], AgentFinish):
            return "exit"
        else:
            return "continue"

    # Define a new graph
    builder = StateGraph(AgentState, Config)

    builder.add_node("agent", agent)
    builder.add_node("tools", execute_tools)

    builder.set_entry_point("agent")

    builder.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "exit": END}
    )

    builder.add_edge("tools", "agent")

    app = builder.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.config_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot

    assert builder.channels.keys() == {"input", "agent_outcome", "intermediate_steps"}

    assert app.invoke({"input": "what is weather in sf"}) == {
        "agent_outcome": AgentFinish(
            return_values={"answer": "answer"}, log="finish:answer"
        ),
        "input": "what is weather in sf",
        "intermediate_steps": [
            (
                AgentAction(
                    tool="search_api", tool_input="query", log="tool:search_api:query"
                ),
                "result for query",
            ),
            (
                AgentAction(
                    tool="search_api",
                    tool_input="another",
                    log="tool:search_api:another",
                ),
                "result for another",
            ),
        ],
    }


def test_conditional_entrypoint_graph_state(snapshot: SnapshotAssertion) -> None:
    class AgentState(TypedDict, total=False):
        input: str
        output: str
        steps: Annotated[list[str], operator.add]

    def left(data: AgentState) -> AgentState:
        return {"output": data["input"] + "->left"}

    def right(data: AgentState) -> AgentState:
        return {"output": data["input"] + "->right"}

    def should_start(data: AgentState) -> str:
        assert data["steps"] == [], "Expected input to be read from the state"
        # Logic to decide where to start
        if len(data["input"]) > 10:
            return "go-right"
        else:
            return "go-left"

    # Define a new graph
    workflow = StateGraph(AgentState)

    workflow.add_node("left", left)
    workflow.add_node("right", right)

    workflow.set_conditional_entry_point(
        should_start, {"go-left": "left", "go-right": "right"}
    )

    workflow.add_conditional_edges("left", lambda data: END, {END: END})
    workflow.add_edge("right", END)

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke({"input": "what is weather in sf"}) == {
        "input": "what is weather in sf",
        "output": "what is weather in sf->right",
        "steps": [],
    }

    assert [*app.stream({"input": "what is weather in sf"})] == [
        {"right": {"output": "what is weather in sf->right"}},
    ]


def test_prebuilt_tool_chat(snapshot: SnapshotAssertion) -> None:
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool

    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    model = FakeChatModel(
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call234",
                        "name": "search_api",
                        "args": {"query": "another"},
                    },
                    {
                        "id": "tool_call567",
                        "name": "search_api",
                        "args": {"query": "a third one"},
                    },
                ],
            ),
            AIMessage(content="answer"),
        ]
    )

    app = create_tool_calling_executor(model, tools)

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke(
        {"messages": [HumanMessage(content="what is weather in sf")]}
    ) == {
        "messages": [
            _AnyIdHumanMessage(content="what is weather in sf"),
            _AnyIdAIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    },
                ],
            ),
            _AnyIdToolMessage(
                content="result for query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            _AnyIdAIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call234",
                        "name": "search_api",
                        "args": {"query": "another"},
                    },
                    {
                        "id": "tool_call567",
                        "name": "search_api",
                        "args": {"query": "a third one"},
                    },
                ],
            ),
            _AnyIdToolMessage(
                content="result for another",
                name="search_api",
                tool_call_id="tool_call234",
            ),
            _AnyIdToolMessage(
                content="result for a third one",
                name="search_api",
                tool_call_id="tool_call567",
                id=AnyStr(),
            ),
            _AnyIdAIMessage(content="answer"),
        ]
    }

    assert [
        c
        for c in app.stream(
            {"messages": [HumanMessage(content="what is weather in sf")]},
            stream_mode="messages",
        )
    ] == [
        (
            _AnyIdAIMessageChunk(
                content="",
                tool_calls=[
                    {
                        "name": "search_api",
                        "args": {"query": "query"},
                        "id": "tool_call123",
                        "type": "tool_call",
                    }
                ],
                tool_call_chunks=[
                    {
                        "name": "search_api",
                        "args": '{"query": "query"}',
                        "id": "tool_call123",
                        "index": None,
                        "type": "tool_call_chunk",
                    }
                ],
            ),
            {
                "langgraph_step": 1,
                "langgraph_node": "agent",
                "langgraph_triggers": ["start:agent"],
                "langgraph_path": ("__pregel_pull", "agent"),
                "langgraph_checkpoint_ns": AnyStr("agent:"),
                "checkpoint_ns": AnyStr("agent:"),
                "ls_provider": "fakechatmodel",
                "ls_model_type": "chat",
            },
        ),
        (
            _AnyIdToolMessage(
                content="result for query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            {
                "langgraph_step": 2,
                "langgraph_node": "tools",
                "langgraph_triggers": ["branch:agent:should_continue:tools"],
                "langgraph_path": ("__pregel_pull", "tools"),
                "langgraph_checkpoint_ns": AnyStr("tools:"),
            },
        ),
        (
            _AnyIdAIMessageChunk(
                content="",
                tool_calls=[
                    {
                        "name": "search_api",
                        "args": {"query": "another"},
                        "id": "tool_call234",
                        "type": "tool_call",
                    },
                    {
                        "name": "search_api",
                        "args": {"query": "a third one"},
                        "id": "tool_call567",
                        "type": "tool_call",
                    },
                ],
                tool_call_chunks=[
                    {
                        "name": "search_api",
                        "args": '{"query": "another"}',
                        "id": "tool_call234",
                        "index": None,
                        "type": "tool_call_chunk",
                    },
                    {
                        "name": "search_api",
                        "args": '{"query": "a third one"}',
                        "id": "tool_call567",
                        "index": None,
                        "type": "tool_call_chunk",
                    },
                ],
            ),
            {
                "langgraph_step": 3,
                "langgraph_node": "agent",
                "langgraph_triggers": ["tools"],
                "langgraph_path": ("__pregel_pull", "agent"),
                "langgraph_checkpoint_ns": AnyStr("agent:"),
                "checkpoint_ns": AnyStr("agent:"),
                "ls_provider": "fakechatmodel",
                "ls_model_type": "chat",
            },
        ),
        (
            _AnyIdToolMessage(
                content="result for another",
                name="search_api",
                tool_call_id="tool_call234",
            ),
            {
                "langgraph_step": 4,
                "langgraph_node": "tools",
                "langgraph_triggers": ["branch:agent:should_continue:tools"],
                "langgraph_path": ("__pregel_pull", "tools"),
                "langgraph_checkpoint_ns": AnyStr("tools:"),
            },
        ),
        (
            _AnyIdToolMessage(
                content="result for a third one",
                name="search_api",
                tool_call_id="tool_call567",
            ),
            {
                "langgraph_step": 4,
                "langgraph_node": "tools",
                "langgraph_triggers": ["branch:agent:should_continue:tools"],
                "langgraph_path": ("__pregel_pull", "tools"),
                "langgraph_checkpoint_ns": AnyStr("tools:"),
            },
        ),
        (
            _AnyIdAIMessageChunk(
                content="answer",
            ),
            {
                "langgraph_step": 5,
                "langgraph_node": "agent",
                "langgraph_triggers": ["tools"],
                "langgraph_path": ("__pregel_pull", "agent"),
                "langgraph_checkpoint_ns": AnyStr("agent:"),
                "checkpoint_ns": AnyStr("agent:"),
                "ls_provider": "fakechatmodel",
                "ls_model_type": "chat",
            },
        ),
    ]

    assert app.invoke(
        {"messages": [HumanMessage(content="what is weather in sf")]},
        {"recursion_limit": 2},
        debug=True,
    ) == {
        "messages": [
            _AnyIdHumanMessage(content="what is weather in sf"),
            _AnyIdAIMessage(content="Sorry, need more steps to process this request."),
        ]
    }

    model.i = 0  # reset the model

    assert (
        app.invoke(
            {"messages": [HumanMessage(content="what is weather in sf")]},
            stream_mode="updates",
        )[0]["agent"]["messages"]
        == [
            {
                "agent": {
                    "messages": [
                        _AnyIdAIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "tool_call123",
                                    "name": "search_api",
                                    "args": {"query": "query"},
                                },
                            ],
                        )
                    ]
                }
            },
            {
                "tools": {
                    "messages": [
                        _AnyIdToolMessage(
                            content="result for query",
                            name="search_api",
                            tool_call_id="tool_call123",
                        )
                    ]
                }
            },
            {
                "agent": {
                    "messages": [
                        _AnyIdAIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "tool_call234",
                                    "name": "search_api",
                                    "args": {"query": "another"},
                                },
                                {
                                    "id": "tool_call567",
                                    "name": "search_api",
                                    "args": {"query": "a third one"},
                                },
                            ],
                        )
                    ]
                }
            },
            {
                "tools": {
                    "messages": [
                        _AnyIdToolMessage(
                            content="result for another",
                            name="search_api",
                            tool_call_id="tool_call234",
                        ),
                        _AnyIdToolMessage(
                            content="result for a third one",
                            name="search_api",
                            tool_call_id="tool_call567",
                        ),
                    ]
                }
            },
            {"agent": {"messages": [_AnyIdAIMessage(content="answer")]}},
        ][0]["agent"]["messages"]
    )

    assert [
        *app.stream({"messages": [HumanMessage(content="what is weather in sf")]})
    ] == [
        {
            "agent": {
                "messages": [
                    _AnyIdAIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "tool_call123",
                                "name": "search_api",
                                "args": {"query": "query"},
                            },
                        ],
                    )
                ]
            }
        },
        {
            "tools": {
                "messages": [
                    _AnyIdToolMessage(
                        content="result for query",
                        name="search_api",
                        tool_call_id="tool_call123",
                    )
                ]
            }
        },
        {
            "agent": {
                "messages": [
                    _AnyIdAIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "tool_call234",
                                "name": "search_api",
                                "args": {"query": "another"},
                            },
                            {
                                "id": "tool_call567",
                                "name": "search_api",
                                "args": {"query": "a third one"},
                            },
                        ],
                    )
                ]
            }
        },
        {
            "tools": {
                "messages": [
                    _AnyIdToolMessage(
                        content="result for another",
                        name="search_api",
                        tool_call_id="tool_call234",
                    ),
                    _AnyIdToolMessage(
                        content="result for a third one",
                        name="search_api",
                        tool_call_id="tool_call567",
                    ),
                ]
            }
        },
        {"agent": {"messages": [_AnyIdAIMessage(content="answer")]}},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_state_graph_packets(
    request: pytest.FixtureRequest, checkpointer_name: str, mocker: MockerFixture
) -> None:
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        ToolCall,
        ToolMessage,
    )
    from langchain_core.tools import tool

    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    class AgentState(TypedDict):
        messages: Annotated[list[BaseMessage], add_messages]
        session: Annotated[httpx.Client, Context(httpx.Client)]

    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]
    tools_by_name = {t.name: t for t in tools}

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                id="ai1",
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    },
                ],
            ),
            AIMessage(
                id="ai2",
                content="",
                tool_calls=[
                    {
                        "id": "tool_call234",
                        "name": "search_api",
                        "args": {"query": "another", "idx": 0},
                    },
                    {
                        "id": "tool_call567",
                        "name": "search_api",
                        "args": {"query": "a third one", "idx": 1},
                    },
                ],
            ),
            AIMessage(id="ai3", content="answer"),
        ]
    )

    def agent(data: AgentState) -> AgentState:
        assert isinstance(data["session"], httpx.Client)
        return {
            "messages": model.invoke(data["messages"]),
            "something_extra": "hi there",
        }

    # Define decision-making logic
    def should_continue(data: AgentState) -> str:
        assert isinstance(data["session"], httpx.Client)
        assert (
            data["something_extra"] == "hi there"
        ), "nodes can pass extra data to their cond edges, which isn't saved in state"
        # Logic to decide whether to continue in the loop or exit
        if tool_calls := data["messages"][-1].tool_calls:
            return [Send("tools", tool_call) for tool_call in tool_calls]
        else:
            return END

    def tools_node(input: ToolCall, config: RunnableConfig) -> AgentState:
        time.sleep(input["args"].get("idx", 0) / 10)
        output = tools_by_name[input["name"]].invoke(input["args"], config)
        return {
            "messages": ToolMessage(
                content=output, name=input["name"], tool_call_id=input["id"]
            )
        }

    # Define a new graph
    workflow = StateGraph(AgentState)

    # Define the two nodes we will cycle between
    workflow.add_node("agent", agent)
    workflow.add_node("tools", tools_node)

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.set_entry_point("agent")

    # We now add a conditional edge
    workflow.add_conditional_edges("agent", should_continue)

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge("tools", "agent")

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable
    app = workflow.compile()

    assert app.invoke({"messages": HumanMessage(content="what is weather in sf")}) == {
        "messages": [
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                id="ai1",
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    },
                ],
            ),
            _AnyIdToolMessage(
                content="result for query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(
                id="ai2",
                content="",
                tool_calls=[
                    {
                        "id": "tool_call234",
                        "name": "search_api",
                        "args": {"query": "another", "idx": 0},
                    },
                    {
                        "id": "tool_call567",
                        "name": "search_api",
                        "args": {"query": "a third one", "idx": 1},
                    },
                ],
            ),
            _AnyIdToolMessage(
                content="result for another",
                name="search_api",
                tool_call_id="tool_call234",
            ),
            _AnyIdToolMessage(
                content="result for a third one",
                name="search_api",
                tool_call_id="tool_call567",
            ),
            AIMessage(content="answer", id="ai3"),
        ]
    }

    assert [
        c
        for c in app.stream(
            {"messages": [HumanMessage(content="what is weather in sf")]}
        )
    ] == [
        {
            "agent": {
                "messages": AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        },
                    ],
                )
            },
        },
        {
            "tools": {
                "messages": _AnyIdToolMessage(
                    content="result for query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            }
        },
        {
            "agent": {
                "messages": AIMessage(
                    id="ai2",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call234",
                            "name": "search_api",
                            "args": {"query": "another", "idx": 0},
                        },
                        {
                            "id": "tool_call567",
                            "name": "search_api",
                            "args": {"query": "a third one", "idx": 1},
                        },
                    ],
                )
            }
        },
        {
            "tools": {
                "messages": _AnyIdToolMessage(
                    content="result for another",
                    name="search_api",
                    tool_call_id="tool_call234",
                )
            },
        },
        {
            "tools": {
                "messages": _AnyIdToolMessage(
                    content="result for a third one",
                    name="search_api",
                    tool_call_id="tool_call567",
                ),
            },
        },
        {"agent": {"messages": AIMessage(content="answer", id="ai3")}},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["agent"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c
        for c in app_w_interrupt.stream(
            {"messages": HumanMessage(content="what is weather in sf")}, config
        )
    ] == [
        {
            "agent": {
                "messages": AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        },
                    ],
                )
            }
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "messages": [
                _AnyIdHumanMessage(content="what is weather in sf"),
                AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        },
                    ],
                ),
            ]
        },
        tasks=(PregelTask(AnyStr(), "tools", (PUSH, 0)),),
        next=("tools",),
        config=(app_w_interrupt.checkpointer.get_tuple(config)).config,
        created_at=(app_w_interrupt.checkpointer.get_tuple(config)).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": {
                    "messages": AIMessage(
                        id="ai1",
                        content="",
                        tool_calls=[
                            {
                                "id": "tool_call123",
                                "name": "search_api",
                                "args": {"query": "query"},
                            },
                        ],
                    )
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # modify ai message
    last_message = (app_w_interrupt.get_state(config)).values["messages"][-1]
    last_message.tool_calls[0]["args"]["query"] = "a different query"
    app_w_interrupt.update_state(
        config, {"messages": last_message, "something_extra": "hi there"}
    )

    # message was replaced instead of appended
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "messages": [
                _AnyIdHumanMessage(content="what is weather in sf"),
                AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        },
                    ],
                ),
            ]
        },
        tasks=(PregelTask(AnyStr(), "tools", (PUSH, 0)),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=(app_w_interrupt.checkpointer.get_tuple(config)).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": {
                    "messages": AIMessage(
                        id="ai1",
                        content="",
                        tool_calls=[
                            {
                                "id": "tool_call123",
                                "name": "search_api",
                                "args": {"query": "a different query"},
                            },
                        ],
                    ),
                    "something_extra": "hi there",
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": {
                "messages": _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            }
        },
        {
            "agent": {
                "messages": AIMessage(
                    id="ai2",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call234",
                            "name": "search_api",
                            "args": {"query": "another", "idx": 0},
                        },
                        {
                            "id": "tool_call567",
                            "name": "search_api",
                            "args": {"query": "a third one", "idx": 1},
                        },
                    ],
                )
            },
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "messages": [
                _AnyIdHumanMessage(content="what is weather in sf"),
                AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        },
                    ],
                ),
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                ),
                AIMessage(
                    id="ai2",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call234",
                            "name": "search_api",
                            "args": {"query": "another", "idx": 0},
                        },
                        {
                            "id": "tool_call567",
                            "name": "search_api",
                            "args": {"query": "a third one", "idx": 1},
                        },
                    ],
                ),
            ]
        },
        tasks=(
            PregelTask(AnyStr(), "tools", (PUSH, 0)),
            PregelTask(AnyStr(), "tools", (PUSH, 1)),
        ),
        next=("tools", "tools"),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=(app_w_interrupt.checkpointer.get_tuple(config)).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 4,
            "writes": {
                "agent": {
                    "messages": AIMessage(
                        id="ai2",
                        content="",
                        tool_calls=[
                            {
                                "id": "tool_call234",
                                "name": "search_api",
                                "args": {"query": "another", "idx": 0},
                            },
                            {
                                "id": "tool_call567",
                                "name": "search_api",
                                "args": {"query": "a third one", "idx": 1},
                            },
                        ],
                    )
                },
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        {
            "messages": AIMessage(content="answer", id="ai2"),
            "something_extra": "hi there",
        },
    )

    # replaces message even if object identity is different, as long as id is the same
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "messages": [
                _AnyIdHumanMessage(content="what is weather in sf"),
                AIMessage(
                    id="ai1",
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        },
                    ],
                ),
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                ),
                AIMessage(content="answer", id="ai2"),
            ]
        },
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=(app_w_interrupt.checkpointer.get_tuple(config)).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {
                "agent": {
                    "messages": AIMessage(content="answer", id="ai2"),
                    "something_extra": "hi there",
                }
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_message_graph(
    snapshot: SnapshotAssertion,
    deterministic_uuids: MockerFixture,
    request: pytest.FixtureRequest,
    checkpointer_name: str,
) -> None:
    from copy import deepcopy

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
    )
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    class FakeFuntionChatModel(FakeMessagesListChatModel):
        def bind_functions(self, functions: list):
            return self

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: Optional[list[str]] = None,
            run_manager: Optional[CallbackManagerForLLMRun] = None,
            **kwargs: Any,
        ) -> ChatResult:
            response = deepcopy(self.responses[self.i])
            if self.i < len(self.responses) - 1:
                self.i += 1
            else:
                self.i = 0
            generation = ChatGeneration(message=response)
            return ChatResult(generations=[generation])

    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    model = FakeFuntionChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
            AIMessage(content="answer", id="ai3"),
        ]
    )

    # Define the function that determines whether to continue or not
    def should_continue(messages):
        last_message = messages[-1]
        # If there is no function call, then we finish
        if not last_message.tool_calls:
            return "end"
        # Otherwise if there is, we continue
        else:
            return "continue"

    # Define a new graph
    workflow = MessageGraph()

    # Define the two nodes we will cycle between
    workflow.add_node("agent", model)
    workflow.add_node("tools", ToolNode(tools))

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.set_entry_point("agent")

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        "agent",
        # Next, we pass in the function that will determine which node is called next.
        should_continue,
        # Finally we pass in a mapping.
        # The keys are strings, and the values are other nodes.
        # END is a special node marking that the graph should finish.
        # What will happen is we will call `should_continue`, and then the output of that
        # will be matched against the keys in this mapping.
        # Based on which one it matches, that node will then be called.
        {
            # If `tools`, then we call the tool node.
            "continue": "tools",
            # Otherwise we finish.
            "end": END,
        },
    )

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge("tools", "agent")

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable
    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert json.dumps(app.get_input_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_output_schema().model_json_schema()) == snapshot
        assert json.dumps(app.get_graph().to_json(), indent=2) == snapshot
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke(HumanMessage(content="what is weather in sf")) == [
        _AnyIdHumanMessage(
            content="what is weather in sf",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "tool_call123",
                    "name": "search_api",
                    "args": {"query": "query"},
                }
            ],
            id="ai1",  # respects ids passed in
        ),
        _AnyIdToolMessage(
            content="result for query",
            name="search_api",
            tool_call_id="tool_call123",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "tool_call456",
                    "name": "search_api",
                    "args": {"query": "another"},
                }
            ],
            id="ai2",
        ),
        _AnyIdToolMessage(
            content="result for another",
            name="search_api",
            tool_call_id="tool_call456",
        ),
        AIMessage(content="answer", id="ai3"),
    ]

    assert [*app.stream([HumanMessage(content="what is weather in sf")])] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for another",
                    name="search_api",
                    tool_call_id="tool_call456",
                )
            ]
        },
        {"agent": AIMessage(content="answer", id="ai3")},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["agent"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c for c in app_w_interrupt.stream(("human", "what is weather in sf"), config)
    ] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # modify ai message
    last_message = app_w_interrupt.get_state(config).values[-1]
    last_message.tool_calls[0]["args"] = {"query": "a different query"}
    next_config = app_w_interrupt.update_state(config, last_message)

    # message was replaced instead of appended
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=next_config,
        created_at=AnyStr(),
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 4,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call456",
                            "name": "search_api",
                            "args": {"query": "another"},
                        }
                    ],
                    id="ai2",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        AIMessage(content="answer", id="ai2"),  # replace existing message
    )

    # replaces message even if object identity is different, as long as id is the same
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(content="answer", id="ai2"),
        ],
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {"agent": AIMessage(content="answer", id="ai2")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["tools"],
    )
    config = {"configurable": {"thread_id": "2"}}
    model.i = 0  # reset the llm

    assert [c for c in app_w_interrupt.stream("what is weather in sf", config)] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # modify ai message
    last_message = app_w_interrupt.get_state(config).values[-1]
    last_message.tool_calls[0]["args"] = {"query": "a different query"}
    app_w_interrupt.update_state(config, last_message)

    # message was replaced instead of appended
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 4,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call456",
                            "name": "search_api",
                            "args": {"query": "another"},
                        }
                    ],
                    id="ai2",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        AIMessage(content="answer", id="ai2"),
    )

    # replaces message even if object identity is different, as long as id is the same
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(content="answer", id="ai2"),
        ],
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {"agent": AIMessage(content="answer", id="ai2")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # add an extra message as if it came from "tools" node
    app_w_interrupt.update_state(config, ("ai", "an extra message"), as_node="tools")

    # extra message is coerced BaseMessge and appended
    # now the next node is "agent" per the graph edges
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(content="answer", id="ai2"),
            _AnyIdAIMessage(content="an extra message"),
        ],
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 6,
            "writes": {"tools": UnsortedSequence("ai", "an extra message")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_root_graph(
    deterministic_uuids: MockerFixture,
    request: pytest.FixtureRequest,
    checkpointer_name: str,
) -> None:
    from copy import deepcopy

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        ToolMessage,
    )
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    class FakeFuntionChatModel(FakeMessagesListChatModel):
        def bind_functions(self, functions: list):
            return self

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: Optional[list[str]] = None,
            run_manager: Optional[CallbackManagerForLLMRun] = None,
            **kwargs: Any,
        ) -> ChatResult:
            response = deepcopy(self.responses[self.i])
            if self.i < len(self.responses) - 1:
                self.i += 1
            else:
                self.i = 0
            generation = ChatGeneration(message=response)
            return ChatResult(generations=[generation])

    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    model = FakeFuntionChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
            AIMessage(content="answer", id="ai3"),
        ]
    )

    # Define the function that determines whether to continue or not
    def should_continue(messages):
        last_message = messages[-1]
        # If there is no function call, then we finish
        if not last_message.tool_calls:
            return "end"
        # Otherwise if there is, we continue
        else:
            return "continue"

    class State(TypedDict):
        __root__: Annotated[list[BaseMessage], add_messages]

    # Define a new graph
    workflow = StateGraph(State)

    # Define the two nodes we will cycle between
    workflow.add_node("agent", model)
    workflow.add_node("tools", ToolNode(tools))

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.set_entry_point("agent")

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        "agent",
        # Next, we pass in the function that will determine which node is called next.
        should_continue,
        # Finally we pass in a mapping.
        # The keys are strings, and the values are other nodes.
        # END is a special node marking that the graph should finish.
        # What will happen is we will call `should_continue`, and then the output of that
        # will be matched against the keys in this mapping.
        # Based on which one it matches, that node will then be called.
        {
            # If `tools`, then we call the tool node.
            "continue": "tools",
            # Otherwise we finish.
            "end": END,
        },
    )

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge("tools", "agent")

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable
    app = workflow.compile()

    assert app.invoke(HumanMessage(content="what is weather in sf")) == [
        _AnyIdHumanMessage(
            content="what is weather in sf",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "tool_call123",
                    "name": "search_api",
                    "args": {"query": "query"},
                }
            ],
            id="ai1",  # respects ids passed in
        ),
        _AnyIdToolMessage(
            content="result for query",
            name="search_api",
            tool_call_id="tool_call123",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "tool_call456",
                    "name": "search_api",
                    "args": {"query": "another"},
                }
            ],
            id="ai2",
        ),
        _AnyIdToolMessage(
            content="result for another",
            name="search_api",
            tool_call_id="tool_call456",
        ),
        AIMessage(content="answer", id="ai3"),
    ]

    assert [*app.stream([HumanMessage(content="what is weather in sf")])] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {
            "tools": [
                ToolMessage(
                    content="result for query",
                    name="search_api",
                    tool_call_id="tool_call123",
                    id="00000000-0000-4000-8000-000000000033",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {
            "tools": [
                ToolMessage(
                    content="result for another",
                    name="search_api",
                    tool_call_id="tool_call456",
                    id="00000000-0000-4000-8000-000000000041",
                )
            ]
        },
        {"agent": AIMessage(content="answer", id="ai3")},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["agent"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c for c in app_w_interrupt.stream(("human", "what is weather in sf"), config)
    ] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # modify ai message
    last_message = app_w_interrupt.get_state(config).values[-1]
    last_message.tool_calls[0]["args"] = {"query": "a different query"}
    next_config = app_w_interrupt.update_state(config, last_message)

    # message was replaced instead of appended
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=next_config,
        created_at=AnyStr(),
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 4,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call456",
                            "name": "search_api",
                            "args": {"query": "another"},
                        }
                    ],
                    id="ai2",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        AIMessage(content="answer", id="ai2"),  # replace existing message
    )

    # replaces message even if object identity is different, as long as id is the same
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(content="answer", id="ai2"),
        ],
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {"agent": AIMessage(content="answer", id="ai2")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["tools"],
    )
    config = {"configurable": {"thread_id": "2"}}
    model.i = 0  # reset the llm

    assert [c for c in app_w_interrupt.stream("what is weather in sf", config)] == [
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    }
                ],
                id="ai1",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # modify ai message
    last_message = app_w_interrupt.get_state(config).values[-1]
    last_message.tool_calls[0]["args"] = {"query": "a different query"}
    app_w_interrupt.update_state(config, last_message)

    # message was replaced instead of appended
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 2,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        }
                    ],
                    id="ai1",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {
            "tools": [
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                )
            ]
        },
        {
            "agent": AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            )
        },
        {"__interrupt__": ()},
    ]

    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call456",
                        "name": "search_api",
                        "args": {"query": "another"},
                    }
                ],
                id="ai2",
            ),
        ],
        tasks=(PregelTask(AnyStr(), "tools", (PULL, "tools")),),
        next=("tools",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 4,
            "writes": {
                "agent": AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tool_call456",
                            "name": "search_api",
                            "args": {"query": "another"},
                        }
                    ],
                    id="ai2",
                )
            },
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    app_w_interrupt.update_state(
        config,
        AIMessage(content="answer", id="ai2"),
    )

    # replaces message even if object identity is different, as long as id is the same
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(content="answer", id="ai2"),
        ],
        tasks=(),
        next=(),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 5,
            "writes": {"agent": AIMessage(content="answer", id="ai2")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # add an extra message as if it came from "tools" node
    app_w_interrupt.update_state(config, ("ai", "an extra message"), as_node="tools")

    # extra message is coerced BaseMessge and appended
    # now the next node is "agent" per the graph edges
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values=[
            _AnyIdHumanMessage(content="what is weather in sf"),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "a different query"},
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
                id=AnyStr(),
            ),
            AIMessage(content="answer", id="ai2"),
            _AnyIdAIMessage(content="an extra message"),
        ],
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 6,
            "writes": {"tools": UnsortedSequence("ai", "an extra message")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # create new graph with one more state key, reuse previous thread history

    def simple_add(left, right):
        if not isinstance(right, list):
            right = [right]
        return left + right

    class MoreState(TypedDict):
        __root__: Annotated[list[BaseMessage], simple_add]
        something_else: str

    # Define a new graph
    new_workflow = StateGraph(MoreState)
    new_workflow.add_node(
        "agent", RunnableMap(__root__=RunnablePick("__root__") | model)
    )
    new_workflow.add_node(
        "tools", RunnableMap(__root__=RunnablePick("__root__") | ToolNode(tools))
    )
    new_workflow.set_entry_point("agent")
    new_workflow.add_conditional_edges(
        "agent",
        RunnablePick("__root__") | should_continue,
        {
            # If `tools`, then we call the tool node.
            "continue": "tools",
            # Otherwise we finish.
            "end": END,
        },
    )
    new_workflow.add_edge("tools", "agent")
    new_app = new_workflow.compile(checkpointer=checkpointer)
    model.i = 0  # reset the llm

    # previous state is converted to new schema
    assert new_app.get_state(config) == StateSnapshot(
        values={
            "__root__": [
                _AnyIdHumanMessage(content="what is weather in sf"),
                AIMessage(
                    content="",
                    id="ai1",
                    tool_calls=[
                        {
                            "id": "tool_call123",
                            "name": "search_api",
                            "args": {"query": "a different query"},
                        }
                    ],
                ),
                _AnyIdToolMessage(
                    content="result for a different query",
                    name="search_api",
                    tool_call_id="tool_call123",
                ),
                AIMessage(content="answer", id="ai2"),
                _AnyIdAIMessage(content="an extra message"),
            ]
        },
        tasks=(PregelTask(AnyStr(), "agent", (PULL, "agent")),),
        next=("agent",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 6,
            "writes": {"tools": UnsortedSequence("ai", "an extra message")},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    # new input is merged to old state
    assert new_app.invoke(
        {
            "__root__": [HumanMessage(content="what is weather in la")],
            "something_else": "value",
        },
        config,
        interrupt_before=["agent"],
    ) == {
        "__root__": [
            HumanMessage(
                content="what is weather in sf",
                id="00000000-0000-4000-8000-000000000070",
            ),
            AIMessage(
                content="",
                id="ai1",
                tool_calls=[
                    {
                        "name": "search_api",
                        "args": {"query": "a different query"},
                        "id": "tool_call123",
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for a different query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            AIMessage(content="answer", id="ai2"),
            AIMessage(
                content="an extra message", id="00000000-0000-4000-8000-000000000091"
            ),
            HumanMessage(content="what is weather in la"),
        ],
        "something_else": "value",
    }


def test_in_one_fan_out_out_one_graph_state() -> None:
    def sorted_add(x: list[str], y: list[str]) -> list[str]:
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def retriever_one(data: State) -> State:
        # timer ensures stream output order is stable
        # also, it confirms that the update order is not dependent on finishing order
        # instead being defined by the order of the nodes/edges in the graph definition
        # ie. stable between invocations
        time.sleep(0.1)
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    workflow = StateGraph(State)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "retriever_one")
    workflow.add_edge("rewrite_query", "retriever_two")
    workflow.add_edge("retriever_one", "qa")
    workflow.add_edge("retriever_two", "qa")
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "query: what is weather in sf",
        "docs": ["doc1", "doc2", "doc3", "doc4"],
        "answer": "doc1,doc2,doc3,doc4",
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]

    assert [*app.stream({"query": "what is weather in sf"}, stream_mode="values")] == [
        {"query": "what is weather in sf", "docs": []},
        {"query": "query: what is weather in sf", "docs": []},
        {
            "query": "query: what is weather in sf",
            "docs": ["doc1", "doc2", "doc3", "doc4"],
        },
        {
            "query": "query: what is weather in sf",
            "docs": ["doc1", "doc2", "doc3", "doc4"],
            "answer": "doc1,doc2,doc3,doc4",
        },
    ]

    assert [
        *app.stream(
            {"query": "what is weather in sf"},
            stream_mode=["values", "updates", "debug"],
        )
    ] == [
        ("values", {"query": "what is weather in sf", "docs": []}),
        (
            "debug",
            {
                "type": "task",
                "timestamp": AnyStr(),
                "step": 1,
                "payload": {
                    "id": AnyStr(),
                    "name": "rewrite_query",
                    "input": {"query": "what is weather in sf", "docs": []},
                    "triggers": ["start:rewrite_query"],
                },
            },
        ),
        ("updates", {"rewrite_query": {"query": "query: what is weather in sf"}}),
        (
            "debug",
            {
                "type": "task_result",
                "timestamp": AnyStr(),
                "step": 1,
                "payload": {
                    "id": AnyStr(),
                    "name": "rewrite_query",
                    "result": [("query", "query: what is weather in sf")],
                    "error": None,
                    "interrupts": [],
                },
            },
        ),
        ("values", {"query": "query: what is weather in sf", "docs": []}),
        (
            "debug",
            {
                "type": "task",
                "timestamp": AnyStr(),
                "step": 2,
                "payload": {
                    "id": AnyStr(),
                    "name": "retriever_one",
                    "input": {"query": "query: what is weather in sf", "docs": []},
                    "triggers": ["rewrite_query"],
                },
            },
        ),
        (
            "debug",
            {
                "type": "task",
                "timestamp": AnyStr(),
                "step": 2,
                "payload": {
                    "id": AnyStr(),
                    "name": "retriever_two",
                    "input": {"query": "query: what is weather in sf", "docs": []},
                    "triggers": ["rewrite_query"],
                },
            },
        ),
        (
            "updates",
            {"retriever_two": {"docs": ["doc3", "doc4"]}},
        ),
        (
            "debug",
            {
                "type": "task_result",
                "timestamp": AnyStr(),
                "step": 2,
                "payload": {
                    "id": AnyStr(),
                    "name": "retriever_two",
                    "result": [("docs", ["doc3", "doc4"])],
                    "error": None,
                    "interrupts": [],
                },
            },
        ),
        (
            "updates",
            {"retriever_one": {"docs": ["doc1", "doc2"]}},
        ),
        (
            "debug",
            {
                "type": "task_result",
                "timestamp": AnyStr(),
                "step": 2,
                "payload": {
                    "id": AnyStr(),
                    "name": "retriever_one",
                    "result": [("docs", ["doc1", "doc2"])],
                    "error": None,
                    "interrupts": [],
                },
            },
        ),
        (
            "values",
            {
                "query": "query: what is weather in sf",
                "docs": ["doc1", "doc2", "doc3", "doc4"],
            },
        ),
        (
            "debug",
            {
                "type": "task",
                "timestamp": AnyStr(),
                "step": 3,
                "payload": {
                    "id": AnyStr(),
                    "name": "qa",
                    "input": {
                        "query": "query: what is weather in sf",
                        "docs": ["doc1", "doc2", "doc3", "doc4"],
                    },
                    "triggers": ["retriever_one", "retriever_two"],
                },
            },
        ),
        ("updates", {"qa": {"answer": "doc1,doc2,doc3,doc4"}}),
        (
            "debug",
            {
                "type": "task_result",
                "timestamp": AnyStr(),
                "step": 3,
                "payload": {
                    "id": AnyStr(),
                    "name": "qa",
                    "result": [("answer", "doc1,doc2,doc3,doc4")],
                    "error": None,
                    "interrupts": [],
                },
            },
        ),
        (
            "values",
            {
                "query": "query: what is weather in sf",
                "answer": "doc1,doc2,doc3,doc4",
                "docs": ["doc1", "doc2", "doc3", "doc4"],
            },
        ),
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_dynamic_interrupt(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")

    class State(TypedDict):
        my_key: Annotated[str, operator.add]
        market: str

    tool_two_node_count = 0

    def tool_two_node(s: State) -> State:
        nonlocal tool_two_node_count
        tool_two_node_count += 1
        if s["market"] == "DE":
            raise NodeInterrupt("Just because...")
        return {"my_key": " all good"}

    tool_two_graph = StateGraph(State)
    tool_two_graph.add_node("tool_two", tool_two_node, retry=RetryPolicy())
    tool_two_graph.add_edge(START, "tool_two")
    tool_two = tool_two_graph.compile()

    tracer = FakeTracer()
    assert tool_two.invoke(
        {"my_key": "value", "market": "DE"}, {"callbacks": [tracer]}
    ) == {
        "my_key": "value",
        "market": "DE",
    }
    assert tool_two_node_count == 1, "interrupts aren't retried"
    assert len(tracer.runs) == 1
    run = tracer.runs[0]
    assert run.end_time is not None
    assert run.error is None
    assert run.outputs == {"market": "DE", "my_key": "value"}

    assert tool_two.invoke({"my_key": "value", "market": "US"}) == {
        "my_key": "value all good",
        "market": "US",
    }

    tool_two = tool_two_graph.compile(checkpointer=checkpointer)

    # missing thread_id
    with pytest.raises(ValueError, match="thread_id"):
        tool_two.invoke({"my_key": "value", "market": "DE"})

    thread1 = {"configurable": {"thread_id": "1"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value ⛰️", "market": "DE"}, thread1) == {
        "my_key": "value ⛰️",
        "market": "DE",
    }
    assert [c.metadata for c in tool_two.checkpointer.list(thread1)] == [
        {
            "parents": {},
            "source": "loop",
            "step": 0,
            "writes": None,
        },
        {
            "parents": {},
            "source": "input",
            "step": -1,
            "writes": {"__start__": {"my_key": "value ⛰️", "market": "DE"}},
        },
    ]
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value ⛰️", "market": "DE"},
        next=("tool_two",),
        tasks=(
            PregelTask(
                AnyStr(),
                "tool_two",
                (PULL, "tool_two"),
                interrupts=(Interrupt("Just because..."),),
            ),
        ),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_start_branch_then(
    snapshot: SnapshotAssertion, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")

    class State(TypedDict):
        my_key: Annotated[str, operator.add]
        market: str
        shared: Annotated[dict[str, dict[str, Any]], SharedValue.on("assistant_id")]

    def assert_shared_value(data: State, config: RunnableConfig) -> State:
        assert "shared" in data
        if thread_id := config["configurable"].get("thread_id"):
            if thread_id == "1":
                # this is the first thread, so should not see a value
                assert data["shared"] == {}
                return {"shared": {"1": {"hello": "world"}}}
            elif thread_id == "2":
                # this should get value saved by thread 1
                assert data["shared"] == {"1": {"hello": "world"}}
            elif thread_id == "3":
                # this is a different assistant, so should not see previous value
                assert data["shared"] == {}
        return {}

    def tool_two_slow(data: State, config: RunnableConfig) -> State:
        return {"my_key": " slow", **assert_shared_value(data, config)}

    def tool_two_fast(data: State, config: RunnableConfig) -> State:
        return {"my_key": " fast", **assert_shared_value(data, config)}

    tool_two_graph = StateGraph(State)
    tool_two_graph.add_node("tool_two_slow", tool_two_slow)
    tool_two_graph.add_node("tool_two_fast", tool_two_fast)
    tool_two_graph.set_conditional_entry_point(
        lambda s: "tool_two_slow" if s["market"] == "DE" else "tool_two_fast", then=END
    )
    tool_two = tool_two_graph.compile()
    assert tool_two.get_graph().draw_mermaid() == snapshot

    assert tool_two.invoke({"my_key": "value", "market": "DE"}) == {
        "my_key": "value slow",
        "market": "DE",
    }
    assert tool_two.invoke({"my_key": "value", "market": "US"}) == {
        "my_key": "value fast",
        "market": "US",
    }

    tool_two = tool_two_graph.compile(
        store=InMemoryStore(),
        checkpointer=checkpointer,
        interrupt_before=["tool_two_fast", "tool_two_slow"],
    )

    # missing thread_id
    with pytest.raises(ValueError, match="thread_id"):
        tool_two.invoke({"my_key": "value", "market": "DE"})

    thread1 = {"configurable": {"thread_id": "1", "assistant_id": "a"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value ⛰️", "market": "DE"}, thread1) == {
        "my_key": "value ⛰️",
        "market": "DE",
    }
    assert [c.metadata for c in tool_two.checkpointer.list(thread1)] == [
        {
            "parents": {},
            "source": "loop",
            "step": 0,
            "writes": None,
        },
        {
            "parents": {},
            "source": "input",
            "step": -1,
            "writes": {"__start__": {"my_key": "value ⛰️", "market": "DE"}},
        },
    ]
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value ⛰️", "market": "DE"},
        tasks=(PregelTask(AnyStr(), "tool_two_slow", (PULL, "tool_two_slow")),),
        next=("tool_two_slow",),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread1, debug=1) == {
        "my_key": "value ⛰️ slow",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value ⛰️ slow", "market": "DE"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"tool_two_slow": {"my_key": " slow"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )

    thread2 = {"configurable": {"thread_id": "2", "assistant_id": "a"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "US"}, thread2) == {
        "my_key": "value",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value", "market": "US"},
        tasks=(PregelTask(AnyStr(), "tool_two_fast", (PULL, "tool_two_fast")),),
        next=("tool_two_fast",),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread2, debug=1) == {
        "my_key": "value fast",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value fast", "market": "US"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"tool_two_fast": {"my_key": " fast"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )

    thread3 = {"configurable": {"thread_id": "3", "assistant_id": "b"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "US"}, thread3) == {
        "my_key": "value",
        "market": "US",
    }
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "value", "market": "US"},
        tasks=(PregelTask(AnyStr(), "tool_two_fast", (PULL, "tool_two_fast")),),
        next=("tool_two_fast",),
        config=tool_two.checkpointer.get_tuple(thread3).config,
        created_at=tool_two.checkpointer.get_tuple(thread3).checkpoint["ts"],
        metadata={"parents": {}, "source": "loop", "step": 0, "writes": None},
        parent_config=[*tool_two.checkpointer.list(thread3, limit=2)][-1].config,
    )
    # update state
    tool_two.update_state(thread3, {"my_key": "key"})  # appends to my_key
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "valuekey", "market": "US"},
        tasks=(PregelTask(AnyStr(), "tool_two_fast", (PULL, "tool_two_fast")),),
        next=("tool_two_fast",),
        config=tool_two.checkpointer.get_tuple(thread3).config,
        created_at=tool_two.checkpointer.get_tuple(thread3).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 1,
            "writes": {START: {"my_key": "key"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread3, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread3, debug=1) == {
        "my_key": "valuekey fast",
        "market": "US",
    }
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "valuekey fast", "market": "US"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread3).config,
        created_at=tool_two.checkpointer.get_tuple(thread3).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 2,
            "writes": {"tool_two_fast": {"my_key": " fast"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread3, limit=2)][-1].config,
    )


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_branch_then(
    snapshot: SnapshotAssertion, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")

    class State(TypedDict):
        my_key: Annotated[str, operator.add]
        market: str

    tool_two_graph = StateGraph(State)
    tool_two_graph.set_entry_point("prepare")
    tool_two_graph.set_finish_point("finish")
    tool_two_graph.add_conditional_edges(
        source="prepare",
        path=lambda s: "tool_two_slow" if s["market"] == "DE" else "tool_two_fast",
        then="finish",
    )
    tool_two_graph.add_node("prepare", lambda s: {"my_key": " prepared"})
    tool_two_graph.add_node("tool_two_slow", lambda s: {"my_key": " slow"})
    tool_two_graph.add_node("tool_two_fast", lambda s: {"my_key": " fast"})
    tool_two_graph.add_node("finish", lambda s: {"my_key": " finished"})
    tool_two = tool_two_graph.compile()
    assert tool_two.get_graph().draw_mermaid(with_styles=False) == snapshot
    assert tool_two.get_graph().draw_mermaid() == snapshot

    assert tool_two.invoke({"my_key": "value", "market": "DE"}, debug=1) == {
        "my_key": "value prepared slow finished",
        "market": "DE",
    }
    assert tool_two.invoke({"my_key": "value", "market": "US"}) == {
        "my_key": "value prepared fast finished",
        "market": "US",
    }

    # test stream_mode=debug
    tool_two = tool_two_graph.compile(checkpointer=checkpointer)
    thread10 = {"configurable": {"thread_id": "10"}}

    res = [
        *tool_two.stream(
            {"my_key": "value", "market": "DE"}, thread10, stream_mode="debug"
        )
    ]

    assert res == [
        {
            "type": "checkpoint",
            "timestamp": AnyStr(),
            "step": -1,
            "payload": {
                "config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "values": {"my_key": ""},
                "metadata": {
                    "parents": {},
                    "source": "input",
                    "step": -1,
                    "writes": {"__start__": {"my_key": "value", "market": "DE"}},
                },
                "parent_config": None,
                "next": ["__start__"],
                "tasks": [
                    {
                        "id": AnyStr(),
                        "name": "__start__",
                        "interrupts": (),
                        "state": None,
                    }
                ],
            },
        },
        {
            "type": "checkpoint",
            "timestamp": AnyStr(),
            "step": 0,
            "payload": {
                "config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "values": {
                    "my_key": "value",
                    "market": "DE",
                },
                "metadata": {
                    "parents": {},
                    "source": "loop",
                    "step": 0,
                    "writes": None,
                },
                "parent_config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "next": ["prepare"],
                "tasks": [
                    {"id": AnyStr(), "name": "prepare", "interrupts": (), "state": None}
                ],
            },
        },
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 1,
            "payload": {
                "id": AnyStr(),
                "name": "prepare",
                "input": {"my_key": "value", "market": "DE"},
                "triggers": ["start:prepare"],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 1,
            "payload": {
                "id": AnyStr(),
                "name": "prepare",
                "result": [("my_key", " prepared")],
                "error": None,
                "interrupts": [],
            },
        },
        {
            "type": "checkpoint",
            "timestamp": AnyStr(),
            "step": 1,
            "payload": {
                "config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "values": {
                    "my_key": "value prepared",
                    "market": "DE",
                },
                "metadata": {
                    "parents": {},
                    "source": "loop",
                    "step": 1,
                    "writes": {"prepare": {"my_key": " prepared"}},
                },
                "parent_config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "next": ["tool_two_slow"],
                "tasks": [
                    {
                        "id": AnyStr(),
                        "name": "tool_two_slow",
                        "interrupts": (),
                        "state": None,
                    }
                ],
            },
        },
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 2,
            "payload": {
                "id": AnyStr(),
                "name": "tool_two_slow",
                "input": {"my_key": "value prepared", "market": "DE"},
                "triggers": ["branch:prepare:condition:tool_two_slow"],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 2,
            "payload": {
                "id": AnyStr(),
                "name": "tool_two_slow",
                "result": [("my_key", " slow")],
                "error": None,
                "interrupts": [],
            },
        },
        {
            "type": "checkpoint",
            "timestamp": AnyStr(),
            "step": 2,
            "payload": {
                "config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "values": {
                    "my_key": "value prepared slow",
                    "market": "DE",
                },
                "metadata": {
                    "parents": {},
                    "source": "loop",
                    "step": 2,
                    "writes": {"tool_two_slow": {"my_key": " slow"}},
                },
                "parent_config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "next": ["finish"],
                "tasks": [
                    {"id": AnyStr(), "name": "finish", "interrupts": (), "state": None}
                ],
            },
        },
        {
            "type": "task",
            "timestamp": AnyStr(),
            "step": 3,
            "payload": {
                "id": AnyStr(),
                "name": "finish",
                "input": {"my_key": "value prepared slow", "market": "DE"},
                "triggers": ["branch:prepare:condition::then"],
            },
        },
        {
            "type": "task_result",
            "timestamp": AnyStr(),
            "step": 3,
            "payload": {
                "id": AnyStr(),
                "name": "finish",
                "result": [("my_key", " finished")],
                "error": None,
                "interrupts": [],
            },
        },
        {
            "type": "checkpoint",
            "timestamp": AnyStr(),
            "step": 3,
            "payload": {
                "config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "values": {
                    "my_key": "value prepared slow finished",
                    "market": "DE",
                },
                "metadata": {
                    "parents": {},
                    "source": "loop",
                    "step": 3,
                    "writes": {"finish": {"my_key": " finished"}},
                },
                "parent_config": {
                    "tags": [],
                    "metadata": {"thread_id": "10"},
                    "callbacks": None,
                    "recursion_limit": 25,
                    "configurable": {
                        "thread_id": "10",
                        "checkpoint_ns": "",
                        "checkpoint_id": AnyStr(),
                    },
                },
                "next": [],
                "tasks": [],
            },
        },
    ]

    tool_two = tool_two_graph.compile(
        checkpointer=checkpointer, interrupt_before=["tool_two_fast", "tool_two_slow"]
    )

    # missing thread_id
    with pytest.raises(ValueError, match="thread_id"):
        tool_two.invoke({"my_key": "value", "market": "DE"})

    thread1 = {"configurable": {"thread_id": "1"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "DE"}, thread1) == {
        "my_key": "value prepared",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value prepared", "market": "DE"},
        tasks=(PregelTask(AnyStr(), "tool_two_slow", (PULL, "tool_two_slow")),),
        next=("tool_two_slow",),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"prepare": {"my_key": " prepared"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread1, debug=1) == {
        "my_key": "value prepared slow finished",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value prepared slow finished", "market": "DE"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 3,
            "writes": {"finish": {"my_key": " finished"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )

    thread2 = {"configurable": {"thread_id": "2"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "US"}, thread2) == {
        "my_key": "value prepared",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value prepared", "market": "US"},
        tasks=(PregelTask(AnyStr(), "tool_two_fast", (PULL, "tool_two_fast")),),
        next=("tool_two_fast",),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"prepare": {"my_key": " prepared"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread2, debug=1) == {
        "my_key": "value prepared fast finished",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value prepared fast finished", "market": "US"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 3,
            "writes": {"finish": {"my_key": " finished"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )

    tool_two = tool_two_graph.compile(
        checkpointer=checkpointer, interrupt_before=["finish"]
    )

    thread1 = {"configurable": {"thread_id": "11"}}

    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "DE"}, thread1) == {
        "my_key": "value prepared slow",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={
            "my_key": "value prepared slow",
            "market": "DE",
        },
        tasks=(PregelTask(AnyStr(), "finish", (PULL, "finish")),),
        next=("finish",),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 2,
            "writes": {"tool_two_slow": {"my_key": " slow"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )

    # update state
    tool_two.update_state(thread1, {"my_key": "er"})
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={
            "my_key": "value prepared slower",
            "market": "DE",
        },
        tasks=(PregelTask(AnyStr(), "finish", (PULL, "finish")),),
        next=("finish",),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 3,
            "writes": {"tool_two_slow": {"my_key": "er"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )

    tool_two = tool_two_graph.compile(
        checkpointer=checkpointer, interrupt_after=["prepare"]
    )

    # missing thread_id
    with pytest.raises(ValueError, match="thread_id"):
        tool_two.invoke({"my_key": "value", "market": "DE"})

    thread1 = {"configurable": {"thread_id": "21"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "DE"}, thread1) == {
        "my_key": "value prepared",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value prepared", "market": "DE"},
        tasks=(PregelTask(AnyStr(), "tool_two_slow", (PULL, "tool_two_slow")),),
        next=("tool_two_slow",),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"prepare": {"my_key": " prepared"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread1, debug=1) == {
        "my_key": "value prepared slow finished",
        "market": "DE",
    }
    assert tool_two.get_state(thread1) == StateSnapshot(
        values={"my_key": "value prepared slow finished", "market": "DE"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread1).config,
        created_at=tool_two.checkpointer.get_tuple(thread1).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 3,
            "writes": {"finish": {"my_key": " finished"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread1, limit=2)][-1].config,
    )

    thread2 = {"configurable": {"thread_id": "22"}}
    # stop when about to enter node
    assert tool_two.invoke({"my_key": "value", "market": "US"}, thread2) == {
        "my_key": "value prepared",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value prepared", "market": "US"},
        tasks=(PregelTask(AnyStr(), "tool_two_fast", (PULL, "tool_two_fast")),),
        next=("tool_two_fast",),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"prepare": {"my_key": " prepared"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread2, debug=1) == {
        "my_key": "value prepared fast finished",
        "market": "US",
    }
    assert tool_two.get_state(thread2) == StateSnapshot(
        values={"my_key": "value prepared fast finished", "market": "US"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread2).config,
        created_at=tool_two.checkpointer.get_tuple(thread2).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 3,
            "writes": {"finish": {"my_key": " finished"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread2, limit=2)][-1].config,
    )

    thread3 = {"configurable": {"thread_id": "23"}}
    # update an empty thread before first run
    uconfig = tool_two.update_state(thread3, {"my_key": "key", "market": "DE"})
    # check current state
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "key", "market": "DE"},
        tasks=(PregelTask(AnyStr(), "prepare", (PULL, "prepare")),),
        next=("prepare",),
        config=uconfig,
        created_at=AnyStr(),
        metadata={
            "parents": {},
            "source": "update",
            "step": 0,
            "writes": {START: {"my_key": "key", "market": "DE"}},
        },
        parent_config=None,
    )
    # run from this point
    assert tool_two.invoke(None, thread3) == {
        "my_key": "key prepared",
        "market": "DE",
    }
    # get state after first node
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "key prepared", "market": "DE"},
        tasks=(PregelTask(AnyStr(), "tool_two_slow", (PULL, "tool_two_slow")),),
        next=("tool_two_slow",),
        config=tool_two.checkpointer.get_tuple(thread3).config,
        created_at=tool_two.checkpointer.get_tuple(thread3).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 1,
            "writes": {"prepare": {"my_key": " prepared"}},
        },
        parent_config=uconfig,
    )
    # resume, for same result as above
    assert tool_two.invoke(None, thread3, debug=1) == {
        "my_key": "key prepared slow finished",
        "market": "DE",
    }
    assert tool_two.get_state(thread3) == StateSnapshot(
        values={"my_key": "key prepared slow finished", "market": "DE"},
        tasks=(),
        next=(),
        config=tool_two.checkpointer.get_tuple(thread3).config,
        created_at=tool_two.checkpointer.get_tuple(thread3).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "loop",
            "step": 3,
            "writes": {"finish": {"my_key": " finished"}},
        },
        parent_config=[*tool_two.checkpointer.list(thread3, limit=2)][-1].config,
    )


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_in_one_fan_out_state_graph_waiting_edge(
    snapshot: SnapshotAssertion, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    workflow = StateGraph(State)

    @workflow.add_node
    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyzer_one(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)  # to ensure stream order
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    workflow.add_node(analyzer_one)
    workflow.add_node(retriever_one)
    workflow.add_node(retriever_two)
    workflow.add_node(qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_edge("rewrite_query", "retriever_two")
    workflow.add_edge(["retriever_one", "retriever_two"], "qa")
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: what is weather in sf",
        "docs": ["doc1", "doc2", "doc3", "doc4"],
        "answer": "doc1,doc2,doc3,doc4",
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["retriever_one"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c for c in app_w_interrupt.stream({"query": "what is weather in sf"}, config)
    ] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"__interrupt__": ()},
    ]

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["qa"],
    )
    config = {"configurable": {"thread_id": "2"}}

    assert [
        c for c in app_w_interrupt.stream({"query": "what is weather in sf"}, config)
    ] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"__interrupt__": ()},
    ]

    app_w_interrupt.update_state(config, {"docs": ["doc5"]})
    assert app_w_interrupt.get_state(config) == StateSnapshot(
        values={
            "query": "analyzed: query: what is weather in sf",
            "docs": ["doc1", "doc2", "doc3", "doc4", "doc5"],
        },
        tasks=(PregelTask(AnyStr(), "qa", (PULL, "qa")),),
        next=("qa",),
        config=app_w_interrupt.checkpointer.get_tuple(config).config,
        created_at=app_w_interrupt.checkpointer.get_tuple(config).checkpoint["ts"],
        metadata={
            "parents": {},
            "source": "update",
            "step": 4,
            "writes": {"retriever_one": {"docs": ["doc5"]}},
        },
        parent_config=[*app_w_interrupt.checkpointer.list(config, limit=2)][-1].config,
    )

    assert [c for c in app_w_interrupt.stream(None, config, debug=1)] == [
        {"qa": {"answer": "doc1,doc2,doc3,doc4,doc5"}},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_in_one_fan_out_state_graph_waiting_edge_via_branch(
    snapshot: SnapshotAssertion, request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyzer_one(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    def rewrite_query_then(data: State) -> Literal["retriever_two"]:
        return "retriever_two"

    workflow = StateGraph(State)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_conditional_edges("rewrite_query", rewrite_query_then)
    workflow.add_edge(["retriever_one", "retriever_two"], "qa")
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot

    assert app.invoke({"query": "what is weather in sf"}, debug=True) == {
        "query": "analyzed: query: what is weather in sf",
        "docs": ["doc1", "doc2", "doc3", "doc4"],
        "answer": "doc1,doc2,doc3,doc4",
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["retriever_one"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c for c in app_w_interrupt.stream({"query": "what is weather in sf"}, config)
    ] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"__interrupt__": ()},
    ]

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_in_one_fan_out_state_graph_waiting_edge_custom_state_class_pydantic1(
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    request: pytest.FixtureRequest,
    checkpointer_name: str,
) -> None:
    from pydantic.v1 import BaseModel, ValidationError

    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    setup = mocker.Mock()
    teardown = mocker.Mock()

    @contextmanager
    def assert_ctx_once() -> Iterator[None]:
        assert setup.call_count == 0
        assert teardown.call_count == 0
        try:
            yield
        finally:
            assert setup.call_count == 1
            assert teardown.call_count == 1
            setup.reset_mock()
            teardown.reset_mock()

    @contextmanager
    def make_httpx_client() -> Iterator[httpx.Client]:
        setup()
        with httpx.Client() as client:
            try:
                yield client
            finally:
                teardown()

    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class InnerObject(BaseModel):
        yo: int

    class State(BaseModel):
        class Config:
            arbitrary_types_allowed = True

        query: str
        inner: InnerObject
        answer: Optional[str] = None
        docs: Annotated[list[str], sorted_add]
        client: Annotated[httpx.Client, Context(make_httpx_client)]

    class Input(BaseModel):
        query: str
        inner: InnerObject

    class Output(BaseModel):
        answer: str
        docs: list[str]

    class StateUpdate(BaseModel):
        query: Optional[str] = None
        answer: Optional[str] = None
        docs: Optional[list[str]] = None

    def rewrite_query(data: State) -> State:
        return {"query": f"query: {data.query}"}

    def analyzer_one(data: State) -> State:
        return StateUpdate(query=f"analyzed: {data.query}")

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data.docs)}

    def decider(data: State) -> str:
        assert isinstance(data, State)
        return "retriever_two"

    workflow = StateGraph(State, input=Input, output=Output)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_conditional_edges(
        "rewrite_query", decider, {"retriever_two": "retriever_two"}
    )
    workflow.add_edge(["retriever_one", "retriever_two"], "qa")
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot
    assert app.get_input_jsonschema() == snapshot
    assert app.get_output_jsonschema() == snapshot

    with pytest.raises(ValidationError), assert_ctx_once():
        app.invoke({"query": {}})

    with assert_ctx_once():
        assert app.invoke({"query": "what is weather in sf", "inner": {"yo": 1}}) == {
            "docs": ["doc1", "doc2", "doc3", "doc4"],
            "answer": "doc1,doc2,doc3,doc4",
        }

    with assert_ctx_once():
        assert [
            *app.stream({"query": "what is weather in sf", "inner": {"yo": 1}})
        ] == [
            {"rewrite_query": {"query": "query: what is weather in sf"}},
            {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
            {"retriever_two": {"docs": ["doc3", "doc4"]}},
            {"retriever_one": {"docs": ["doc1", "doc2"]}},
            {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
        ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["retriever_one"],
    )
    config = {"configurable": {"thread_id": "1"}}

    with assert_ctx_once():
        assert [
            c
            for c in app_w_interrupt.stream(
                {"query": "what is weather in sf", "inner": {"yo": 1}}, config
            )
        ] == [
            {"rewrite_query": {"query": "query: what is weather in sf"}},
            {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
            {"retriever_two": {"docs": ["doc3", "doc4"]}},
            {"retriever_one": {"docs": ["doc1", "doc2"]}},
            {"__interrupt__": ()},
        ]

    with assert_ctx_once():
        assert [c for c in app_w_interrupt.stream(None, config)] == [
            {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
        ]

    with assert_ctx_once():
        assert app_w_interrupt.update_state(
            config, {"docs": ["doc5"]}, as_node="rewrite_query"
        ) == {
            "configurable": {
                "thread_id": "1",
                "checkpoint_id": AnyStr(),
                "checkpoint_ns": "",
            }
        }


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_in_one_fan_out_state_graph_waiting_edge_custom_state_class_pydantic2(
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    request: pytest.FixtureRequest,
    checkpointer_name: str,
) -> None:
    from pydantic import BaseModel, ConfigDict, ValidationError

    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    setup = mocker.Mock()
    teardown = mocker.Mock()

    @contextmanager
    def assert_ctx_once() -> Iterator[None]:
        assert setup.call_count == 0
        assert teardown.call_count == 0
        try:
            yield
        finally:
            assert setup.call_count == 1
            assert teardown.call_count == 1
            setup.reset_mock()
            teardown.reset_mock()

    @contextmanager
    def make_httpx_client() -> Iterator[httpx.Client]:
        setup()
        with httpx.Client() as client:
            try:
                yield client
            finally:
                teardown()

    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class InnerObject(BaseModel):
        yo: int

    class State(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        query: str
        inner: InnerObject
        answer: Optional[str] = None
        docs: Annotated[list[str], sorted_add]
        client: Annotated[httpx.Client, Context(make_httpx_client)]

    class StateUpdate(BaseModel):
        query: Optional[str] = None
        answer: Optional[str] = None
        docs: Optional[list[str]] = None

    class Input(BaseModel):
        query: str
        inner: InnerObject

    class Output(BaseModel):
        answer: str
        docs: list[str]

    def rewrite_query(data: State) -> State:
        return {"query": f"query: {data.query}"}

    def analyzer_one(data: State) -> State:
        return StateUpdate(query=f"analyzed: {data.query}")

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data.docs)}

    def decider(data: State) -> str:
        assert isinstance(data, State)
        return "retriever_two"

    workflow = StateGraph(State, input=Input, output=Output)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_conditional_edges(
        "rewrite_query", decider, {"retriever_two": "retriever_two"}
    )
    workflow.add_edge(["retriever_one", "retriever_two"], "qa")
    workflow.set_finish_point("qa")

    app = workflow.compile()

    if SHOULD_CHECK_SNAPSHOTS:
        assert app.get_graph().draw_mermaid(with_styles=False) == snapshot
        assert app.get_input_schema().model_json_schema() == snapshot
        assert app.get_output_schema().model_json_schema() == snapshot

    with pytest.raises(ValidationError), assert_ctx_once():
        app.invoke({"query": {}})

    with assert_ctx_once():
        assert app.invoke({"query": "what is weather in sf", "inner": {"yo": 1}}) == {
            "docs": ["doc1", "doc2", "doc3", "doc4"],
            "answer": "doc1,doc2,doc3,doc4",
        }

    with assert_ctx_once():
        assert [
            *app.stream({"query": "what is weather in sf", "inner": {"yo": 1}})
        ] == [
            {"rewrite_query": {"query": "query: what is weather in sf"}},
            {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
            {"retriever_two": {"docs": ["doc3", "doc4"]}},
            {"retriever_one": {"docs": ["doc1", "doc2"]}},
            {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
        ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["retriever_one"],
    )
    config = {"configurable": {"thread_id": "1"}}

    with assert_ctx_once():
        assert [
            c
            for c in app_w_interrupt.stream(
                {"query": "what is weather in sf", "inner": {"yo": 1}}, config
            )
        ] == [
            {"rewrite_query": {"query": "query: what is weather in sf"}},
            {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
            {"retriever_two": {"docs": ["doc3", "doc4"]}},
            {"retriever_one": {"docs": ["doc1", "doc2"]}},
            {"__interrupt__": ()},
        ]

    with assert_ctx_once():
        assert [c for c in app_w_interrupt.stream(None, config)] == [
            {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
        ]

    with assert_ctx_once():
        assert app_w_interrupt.update_state(
            config, {"docs": ["doc5"]}, as_node="rewrite_query"
        ) == {
            "configurable": {
                "thread_id": "1",
                "checkpoint_id": AnyStr(),
                "checkpoint_ns": "",
            }
        }


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_in_one_fan_out_state_graph_waiting_edge_plus_regular(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer: BaseCheckpointSaver = request.getfixturevalue(
        f"checkpointer_{checkpointer_name}"
    )

    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyzer_one(data: State) -> State:
        time.sleep(0.1)
        return {"query": f'analyzed: {data["query"]}'}

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.2)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    workflow = StateGraph(State)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_edge("rewrite_query", "retriever_two")
    workflow.add_edge(["retriever_one", "retriever_two"], "qa")
    workflow.set_finish_point("qa")

    # silly edge, to make sure having been triggered before doesn't break
    # semantics of named barrier (== waiting edges)
    workflow.add_edge("rewrite_query", "qa")

    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: what is weather in sf",
        "docs": ["doc1", "doc2", "doc3", "doc4"],
        "answer": "doc1,doc2,doc3,doc4",
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"qa": {"answer": ""}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]

    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer,
        interrupt_after=["retriever_one"],
    )
    config = {"configurable": {"thread_id": "1"}}

    assert [
        c for c in app_w_interrupt.stream({"query": "what is weather in sf"}, config)
    ] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"qa": {"answer": ""}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"__interrupt__": ()},
    ]

    assert [c for c in app_w_interrupt.stream(None, config)] == [
        {"qa": {"answer": "doc1,doc2,doc3,doc4"}},
    ]


def test_in_one_fan_out_state_graph_waiting_edge_multiple() -> None:
    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyzer_one(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    def decider(data: State) -> None:
        return None

    def decider_cond(data: State) -> str:
        if data["query"].count("analyzed") > 1:
            return "qa"
        else:
            return "rewrite_query"

    workflow = StateGraph(State)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("decider", decider)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_edge("rewrite_query", "analyzer_one")
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_edge("rewrite_query", "retriever_two")
    workflow.add_edge(["retriever_one", "retriever_two"], "decider")
    workflow.add_conditional_edges("decider", decider_cond)
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: analyzed: query: what is weather in sf",
        "answer": "doc1,doc1,doc2,doc2,doc3,doc3,doc4,doc4",
        "docs": ["doc1", "doc1", "doc2", "doc2", "doc3", "doc3", "doc4", "doc4"],
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"decider": None},
        {"rewrite_query": {"query": "query: analyzed: query: what is weather in sf"}},
        {
            "analyzer_one": {
                "query": "analyzed: query: analyzed: query: what is weather in sf"
            }
        },
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"decider": None},
        {"qa": {"answer": "doc1,doc1,doc2,doc2,doc3,doc3,doc4,doc4"}},
    ]


def test_callable_in_conditional_edges_with_no_path_map() -> None:
    class State(TypedDict, total=False):
        query: str

    def rewrite(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyze(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    class ChooseAnalyzer:
        def __call__(self, data: State) -> str:
            return "analyzer"

    workflow = StateGraph(State)
    workflow.add_node("rewriter", rewrite)
    workflow.add_node("analyzer", analyze)
    workflow.add_conditional_edges("rewriter", ChooseAnalyzer())
    workflow.set_entry_point("rewriter")
    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: what is weather in sf",
    }


def test_function_in_conditional_edges_with_no_path_map() -> None:
    class State(TypedDict, total=False):
        query: str

    def rewrite(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def analyze(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    def choose_analyzer(data: State) -> str:
        return "analyzer"

    workflow = StateGraph(State)
    workflow.add_node("rewriter", rewrite)
    workflow.add_node("analyzer", analyze)
    workflow.add_conditional_edges("rewriter", choose_analyzer)
    workflow.set_entry_point("rewriter")
    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: what is weather in sf",
    }


def test_in_one_fan_out_state_graph_waiting_edge_multiple_cond_edge() -> None:
    def sorted_add(
        x: list[str], y: Union[list[str], list[tuple[str, str]]]
    ) -> list[str]:
        if isinstance(y[0], tuple):
            for rem, _ in y:
                x.remove(rem)
            y = [t[1] for t in y]
        return sorted(operator.add(x, y))

    class State(TypedDict, total=False):
        query: str
        answer: str
        docs: Annotated[list[str], sorted_add]

    def rewrite_query(data: State) -> State:
        return {"query": f'query: {data["query"]}'}

    def retriever_picker(data: State) -> list[str]:
        return ["analyzer_one", "retriever_two"]

    def analyzer_one(data: State) -> State:
        return {"query": f'analyzed: {data["query"]}'}

    def retriever_one(data: State) -> State:
        return {"docs": ["doc1", "doc2"]}

    def retriever_two(data: State) -> State:
        time.sleep(0.1)
        return {"docs": ["doc3", "doc4"]}

    def qa(data: State) -> State:
        return {"answer": ",".join(data["docs"])}

    def decider(data: State) -> None:
        return None

    def decider_cond(data: State) -> str:
        if data["query"].count("analyzed") > 1:
            return "qa"
        else:
            return "rewrite_query"

    workflow = StateGraph(State)

    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("analyzer_one", analyzer_one)
    workflow.add_node("retriever_one", retriever_one)
    workflow.add_node("retriever_two", retriever_two)
    workflow.add_node("decider", decider)
    workflow.add_node("qa", qa)

    workflow.set_entry_point("rewrite_query")
    workflow.add_conditional_edges("rewrite_query", retriever_picker)
    workflow.add_edge("analyzer_one", "retriever_one")
    workflow.add_edge(["retriever_one", "retriever_two"], "decider")
    workflow.add_conditional_edges("decider", decider_cond)
    workflow.set_finish_point("qa")

    app = workflow.compile()

    assert app.invoke({"query": "what is weather in sf"}) == {
        "query": "analyzed: query: analyzed: query: what is weather in sf",
        "answer": "doc1,doc1,doc2,doc2,doc3,doc3,doc4,doc4",
        "docs": ["doc1", "doc1", "doc2", "doc2", "doc3", "doc3", "doc4", "doc4"],
    }

    assert [*app.stream({"query": "what is weather in sf"})] == [
        {"rewrite_query": {"query": "query: what is weather in sf"}},
        {"analyzer_one": {"query": "analyzed: query: what is weather in sf"}},
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"decider": None},
        {"rewrite_query": {"query": "query: analyzed: query: what is weather in sf"}},
        {
            "analyzer_one": {
                "query": "analyzed: query: analyzed: query: what is weather in sf"
            }
        },
        {"retriever_two": {"docs": ["doc3", "doc4"]}},
        {"retriever_one": {"docs": ["doc1", "doc2"]}},
        {"decider": None},
        {"qa": {"answer": "doc1,doc1,doc2,doc2,doc3,doc3,doc4,doc4"}},
    ]


def test_simple_multi_edge(snapshot: SnapshotAssertion) -> None:
    class State(TypedDict):
        my_key: Annotated[str, operator.add]

    def up(state: State):
        pass

    def side(state: State):
        pass

    def other(state: State):
        return {"my_key": "_more"}

    def down(state: State):
        pass

    graph = StateGraph(State)

    graph.add_node("up", up)
    graph.add_node("side", side)
    graph.add_node("other", other)
    graph.add_node("down", down)

    graph.set_entry_point("up")
    graph.add_edge("up", "side")
    graph.add_edge("up", "other")
    graph.add_edge(["up", "side"], "down")
    graph.set_finish_point("down")

    app = graph.compile()

    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot
    assert app.invoke({"my_key": "my_value"}) == {"my_key": "my_value_more"}
    assert [*app.stream({"my_key": "my_value"})] in (
        [
            {"up": None},
            {"side": None},
            {"other": {"my_key": "_more"}},
            {"down": None},
        ],
        [
            {"up": None},
            {"other": {"my_key": "_more"}},
            {"side": None},
            {"down": None},
        ],
    )


def test_nested_graph_xray(snapshot: SnapshotAssertion) -> None:
    class State(TypedDict):
        my_key: Annotated[str, operator.add]
        market: str

    def logic(state: State):
        pass

    tool_two_graph = StateGraph(State)
    tool_two_graph.add_node("tool_two_slow", logic)
    tool_two_graph.add_node("tool_two_fast", logic)
    tool_two_graph.set_conditional_entry_point(
        lambda s: "tool_two_slow" if s["market"] == "DE" else "tool_two_fast",
        then=END,
    )
    tool_two = tool_two_graph.compile()

    graph = StateGraph(State)
    graph.add_node("tool_one", logic)
    graph.add_node("tool_two", tool_two)
    graph.add_node("tool_three", logic)
    graph.set_conditional_entry_point(lambda s: "tool_one", then=END)
    app = graph.compile()

    assert app.get_graph(xray=True).to_json() == snapshot
    assert app.get_graph(xray=True).draw_mermaid() == snapshot


def test_nested_graph(snapshot: SnapshotAssertion) -> None:
    def never_called_fn(state: Any):
        assert 0, "This function should never be called"

    never_called = RunnableLambda(never_called_fn)

    class InnerState(TypedDict):
        my_key: str
        my_other_key: str

    def up(state: InnerState):
        return {"my_key": state["my_key"] + " there", "my_other_key": state["my_key"]}

    inner = StateGraph(InnerState)
    inner.add_node("up", up)
    inner.set_entry_point("up")
    inner.set_finish_point("up")

    class State(TypedDict):
        my_key: str
        never_called: Any

    def side(state: State):
        return {"my_key": state["my_key"] + " and back again"}

    graph = StateGraph(State)
    graph.add_node("inner", inner.compile())
    graph.add_node("side", side)
    graph.set_entry_point("inner")
    graph.add_edge("inner", "side")
    graph.set_finish_point("side")

    app = graph.compile()

    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot
    assert app.get_graph(xray=True).draw_mermaid() == snapshot
    assert app.invoke(
        {"my_key": "my value", "never_called": never_called}, debug=True
    ) == {
        "my_key": "my value there and back again",
        "never_called": never_called,
    }
    assert [*app.stream({"my_key": "my value", "never_called": never_called})] == [
        {"inner": {"my_key": "my value there"}},
        {"side": {"my_key": "my value there and back again"}},
    ]
    assert [
        *app.stream(
            {"my_key": "my value", "never_called": never_called}, stream_mode="values"
        )
    ] == [
        {
            "my_key": "my value",
            "never_called": never_called,
        },
        {
            "my_key": "my value there",
            "never_called": never_called,
        },
        {
            "my_key": "my value there and back again",
            "never_called": never_called,
        },
    ]

    chain = app | RunnablePassthrough()

    assert chain.invoke({"my_key": "my value", "never_called": never_called}) == {
        "my_key": "my value there and back again",
        "never_called": never_called,
    }
    assert [*chain.stream({"my_key": "my value", "never_called": never_called})] == [
        {"inner": {"my_key": "my value there"}},
        {"side": {"my_key": "my value there and back again"}},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_stream_subgraphs_during_execution(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class InnerState(TypedDict):
        my_key: Annotated[str, operator.add]
        my_other_key: str

    def inner_1(state: InnerState):
        return {"my_key": "got here", "my_other_key": state["my_key"]}

    def inner_2(state: InnerState):
        time.sleep(0.5)
        return {
            "my_key": " and there",
            "my_other_key": state["my_key"],
        }

    inner = StateGraph(InnerState)
    inner.add_node("inner_1", inner_1)
    inner.add_node("inner_2", inner_2)
    inner.add_edge("inner_1", "inner_2")
    inner.set_entry_point("inner_1")
    inner.set_finish_point("inner_2")

    class State(TypedDict):
        my_key: Annotated[str, operator.add]

    def outer_1(state: State):
        time.sleep(0.2)
        return {"my_key": " and parallel"}

    def outer_2(state: State):
        return {"my_key": " and back again"}

    graph = StateGraph(State)
    graph.add_node("inner", inner.compile())
    graph.add_node("outer_1", outer_1)
    graph.add_node("outer_2", outer_2)

    graph.add_edge(START, "inner")
    graph.add_edge(START, "outer_1")
    graph.add_edge(["inner", "outer_1"], "outer_2")
    graph.add_edge("outer_2", END)

    app = graph.compile(checkpointer=checkpointer)

    start = time.perf_counter()
    chunks: list[tuple[float, Any]] = []
    config = {"configurable": {"thread_id": "2"}}
    for c in app.stream({"my_key": ""}, config, subgraphs=True):
        chunks.append((round(time.perf_counter() - start, 1), c))
    for idx in range(len(chunks)):
        elapsed, c = chunks[idx]
        chunks[idx] = (round(elapsed - chunks[0][0], 1), c)

    assert chunks == [
        # arrives before "inner" finishes
        (
            FloatBetween(0.0, 0.1),
            (
                (AnyStr("inner:"),),
                {"inner_1": {"my_key": "got here", "my_other_key": ""}},
            ),
        ),
        (FloatBetween(0.2, 0.3), ((), {"outer_1": {"my_key": " and parallel"}})),
        (
            FloatBetween(0.5, 0.6),
            (
                (AnyStr("inner:"),),
                {"inner_2": {"my_key": " and there", "my_other_key": "got here"}},
            ),
        ),
        (FloatBetween(0.5, 0.6), ((), {"inner": {"my_key": "got here and there"}})),
        (FloatBetween(0.5, 0.6), ((), {"outer_2": {"my_key": " and back again"}})),
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_stream_buffering_single_node(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class State(TypedDict):
        my_key: Annotated[str, operator.add]

    def node(state: State, writer: StreamWriter):
        writer("Before sleep")
        time.sleep(0.2)
        writer("After sleep")
        return {"my_key": "got here"}

    builder = StateGraph(State)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    graph = builder.compile(checkpointer=checkpointer)

    start = time.perf_counter()
    chunks: list[tuple[float, Any]] = []
    config = {"configurable": {"thread_id": "2"}}
    for c in graph.stream({"my_key": ""}, config, stream_mode="custom"):
        chunks.append((round(time.perf_counter() - start, 1), c))

    assert chunks == [
        (FloatBetween(0.0, 0.1), "Before sleep"),
        (FloatBetween(0.2, 0.3), "After sleep"),
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_nested_graph_interrupts_parallel(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class InnerState(TypedDict):
        my_key: Annotated[str, operator.add]
        my_other_key: str

    def inner_1(state: InnerState):
        time.sleep(0.1)
        return {"my_key": "got here", "my_other_key": state["my_key"]}

    def inner_2(state: InnerState):
        return {
            "my_key": " and there",
            "my_other_key": state["my_key"],
        }

    inner = StateGraph(InnerState)
    inner.add_node("inner_1", inner_1)
    inner.add_node("inner_2", inner_2)
    inner.add_edge("inner_1", "inner_2")
    inner.set_entry_point("inner_1")
    inner.set_finish_point("inner_2")

    class State(TypedDict):
        my_key: Annotated[str, operator.add]

    def outer_1(state: State):
        return {"my_key": " and parallel"}

    def outer_2(state: State):
        return {"my_key": " and back again"}

    graph = StateGraph(State)
    graph.add_node("inner", inner.compile(interrupt_before=["inner_2"]))
    graph.add_node("outer_1", outer_1)
    graph.add_node("outer_2", outer_2)

    graph.add_edge(START, "inner")
    graph.add_edge(START, "outer_1")
    graph.add_edge(["inner", "outer_1"], "outer_2")
    graph.set_finish_point("outer_2")

    app = graph.compile(checkpointer=checkpointer)

    # test invoke w/ nested interrupt
    config = {"configurable": {"thread_id": "1"}}
    assert app.invoke({"my_key": ""}, config, debug=True) == {
        "my_key": "",
    }

    assert app.invoke(None, config, debug=True) == {
        "my_key": "got here and there and parallel and back again",
    }

    # below combo of assertions is asserting two things
    # - outer_1 finishes before inner interrupts (because we see its output in stream, which only happens after node finishes)
    # - the writes of outer are persisted in 1st call and used in 2nd call, ie outer isn't called again (because we dont see outer_1 output again in 2nd stream)
    # test stream updates w/ nested interrupt
    config = {"configurable": {"thread_id": "2"}}
    assert [*app.stream({"my_key": ""}, config, subgraphs=True)] == [
        # we got to parallel node first
        ((), {"outer_1": {"my_key": " and parallel"}}),
        ((AnyStr("inner:"),), {"inner_1": {"my_key": "got here", "my_other_key": ""}}),
        ((), {"__interrupt__": ()}),
    ]
    assert [*app.stream(None, config)] == [
        {"outer_1": {"my_key": " and parallel"}, "__metadata__": {"cached": True}},
        {"inner": {"my_key": "got here and there"}},
        {"outer_2": {"my_key": " and back again"}},
    ]

    # test stream values w/ nested interrupt
    config = {"configurable": {"thread_id": "3"}}
    assert [*app.stream({"my_key": ""}, config, stream_mode="values")] == [
        {"my_key": ""},
    ]
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": ""},
        {"my_key": "got here and there and parallel"},
        {"my_key": "got here and there and parallel and back again"},
    ]

    # test interrupts BEFORE the parallel node
    app = graph.compile(checkpointer=checkpointer, interrupt_before=["outer_1"])
    config = {"configurable": {"thread_id": "4"}}
    assert [*app.stream({"my_key": ""}, config, stream_mode="values")] == [
        {"my_key": ""}
    ]
    # while we're waiting for the node w/ interrupt inside to finish
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": ""},
    ]
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": ""},
        {"my_key": "got here and there and parallel"},
        {"my_key": "got here and there and parallel and back again"},
    ]

    # test interrupts AFTER the parallel node
    app = graph.compile(checkpointer=checkpointer, interrupt_after=["outer_1"])
    config = {"configurable": {"thread_id": "5"}}
    assert [*app.stream({"my_key": ""}, config, stream_mode="values")] == [
        {"my_key": ""}
    ]
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": ""},
        {"my_key": "got here and there and parallel"},
    ]
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": "got here and there and parallel"},
        {"my_key": "got here and there and parallel and back again"},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_doubly_nested_graph_interrupts(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class State(TypedDict):
        my_key: str

    class ChildState(TypedDict):
        my_key: str

    class GrandChildState(TypedDict):
        my_key: str

    def grandchild_1(state: ChildState):
        return {"my_key": state["my_key"] + " here"}

    def grandchild_2(state: ChildState):
        return {
            "my_key": state["my_key"] + " and there",
        }

    grandchild = StateGraph(GrandChildState)
    grandchild.add_node("grandchild_1", grandchild_1)
    grandchild.add_node("grandchild_2", grandchild_2)
    grandchild.add_edge("grandchild_1", "grandchild_2")
    grandchild.set_entry_point("grandchild_1")
    grandchild.set_finish_point("grandchild_2")

    child = StateGraph(ChildState)
    child.add_node(
        "child_1",
        grandchild.compile(interrupt_before=["grandchild_2"]),
    )
    child.set_entry_point("child_1")
    child.set_finish_point("child_1")

    def parent_1(state: State):
        return {"my_key": "hi " + state["my_key"]}

    def parent_2(state: State):
        return {"my_key": state["my_key"] + " and back again"}

    graph = StateGraph(State)
    graph.add_node("parent_1", parent_1)
    graph.add_node("child", child.compile())
    graph.add_node("parent_2", parent_2)
    graph.set_entry_point("parent_1")
    graph.add_edge("parent_1", "child")
    graph.add_edge("child", "parent_2")
    graph.set_finish_point("parent_2")

    app = graph.compile(checkpointer=checkpointer)

    # test invoke w/ nested interrupt
    config = {"configurable": {"thread_id": "1"}}
    assert app.invoke({"my_key": "my value"}, config, debug=True) == {
        "my_key": "hi my value",
    }

    assert app.invoke(None, config, debug=True) == {
        "my_key": "hi my value here and there and back again",
    }

    # test stream updates w/ nested interrupt
    config = {"configurable": {"thread_id": "2"}}
    assert [*app.stream({"my_key": "my value"}, config)] == [
        {"parent_1": {"my_key": "hi my value"}},
        {"__interrupt__": ()},
    ]
    assert [*app.stream(None, config)] == [
        {"child": {"my_key": "hi my value here and there"}},
        {"parent_2": {"my_key": "hi my value here and there and back again"}},
    ]

    # test stream values w/ nested interrupt
    config = {"configurable": {"thread_id": "3"}}
    assert [*app.stream({"my_key": "my value"}, config, stream_mode="values")] == [
        {"my_key": "my value"},
        {"my_key": "hi my value"},
    ]
    assert [*app.stream(None, config, stream_mode="values")] == [
        {"my_key": "hi my value"},
        {"my_key": "hi my value here and there"},
        {"my_key": "hi my value here and there and back again"},
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_nested_graph_state(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class InnerState(TypedDict):
        my_key: str
        my_other_key: str

    def inner_1(state: InnerState):
        return {
            "my_key": state["my_key"] + " here",
            "my_other_key": state["my_key"],
        }

    def inner_2(state: InnerState):
        return {
            "my_key": state["my_key"] + " and there",
            "my_other_key": state["my_key"],
        }

    inner = StateGraph(InnerState)
    inner.add_node("inner_1", inner_1)
    inner.add_node("inner_2", inner_2)
    inner.add_edge("inner_1", "inner_2")
    inner.set_entry_point("inner_1")
    inner.set_finish_point("inner_2")

    class State(TypedDict):
        my_key: str
        other_parent_key: str

    def outer_1(state: State):
        return {"my_key": "hi " + state["my_key"]}

    def outer_2(state: State):
        return {"my_key": state["my_key"] + " and back again"}

    graph = StateGraph(State)
    graph.add_node("outer_1", outer_1)
    graph.add_node(
        "inner",
        inner.compile(interrupt_before=["inner_2"]),
    )
    graph.add_node("outer_2", outer_2)
    graph.set_entry_point("outer_1")
    graph.add_edge("outer_1", "inner")
    graph.add_edge("inner", "outer_2")
    graph.set_finish_point("outer_2")

    app = graph.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "1"}}
    app.invoke({"my_key": "my value"}, config, debug=True)
    # test state w/ nested subgraph state (right after interrupt)
    # first get_state without subgraph state
    assert app.get_state(config) == StateSnapshot(
        values={"my_key": "hi my value"},
        tasks=(
            PregelTask(
                AnyStr(),
                "inner",
                (PULL, "inner"),
                state={"configurable": {"thread_id": "1", "checkpoint_ns": AnyStr()}},
            ),
        ),
        next=("inner",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {"outer_1": {"my_key": "hi my value"}},
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    # now, get_state with subgraphs state
    assert app.get_state(config, subgraphs=True) == StateSnapshot(
        values={"my_key": "hi my value"},
        tasks=(
            PregelTask(
                AnyStr(),
                "inner",
                (PULL, "inner"),
                state=StateSnapshot(
                    values={
                        "my_key": "hi my value here",
                        "my_other_key": "hi my value",
                    },
                    tasks=(
                        PregelTask(
                            AnyStr(),
                            "inner_2",
                            (PULL, "inner_2"),
                        ),
                    ),
                    next=("inner_2",),
                    config={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("inner:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {"": AnyStr(), AnyStr("child:"): AnyStr()}
                            ),
                        }
                    },
                    metadata={
                        "parents": {
                            "": AnyStr(),
                        },
                        "source": "loop",
                        "writes": {
                            "inner_1": {
                                "my_key": "hi my value here",
                                "my_other_key": "hi my value",
                            }
                        },
                        "step": 1,
                    },
                    created_at=AnyStr(),
                    parent_config={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("inner:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {"": AnyStr(), AnyStr("child:"): AnyStr()}
                            ),
                        }
                    },
                ),
            ),
        ),
        next=("inner",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {"outer_1": {"my_key": "hi my value"}},
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    # get_state_history returns outer graph checkpoints
    history = list(app.get_state_history(config))
    assert history == [
        StateSnapshot(
            values={"my_key": "hi my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "inner",
                    (PULL, "inner"),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("inner:"),
                        }
                    },
                ),
            ),
            next=("inner",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {"outer_1": {"my_key": "hi my value"}},
                "step": 1,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "outer_1",
                    (PULL, "outer_1"),
                    result={"my_key": "hi my value"},
                ),
            ),
            next=("outer_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "writes": None, "step": 0},
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "__start__",
                    (PULL, "__start__"),
                    result={"my_key": "my value"},
                ),
            ),
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "writes": {"__start__": {"my_key": "my value"}},
                "step": -1,
            },
            created_at=AnyStr(),
            parent_config=None,
        ),
    ]
    # get_state_history for a subgraph returns its checkpoints
    child_history = [*app.get_state_history(history[0].tasks[0].state)]
    assert child_history == [
        StateSnapshot(
            values={"my_key": "hi my value here", "my_other_key": "hi my value"},
            next=("inner_2",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("inner:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": {
                    "inner_1": {
                        "my_key": "hi my value here",
                        "my_other_key": "hi my value",
                    }
                },
                "step": 1,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("inner:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            tasks=(PregelTask(AnyStr(), "inner_2", (PULL, "inner_2")),),
        ),
        StateSnapshot(
            values={"my_key": "hi my value"},
            next=("inner_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("inner:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": None,
                "step": 0,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("inner:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            tasks=(
                PregelTask(
                    AnyStr(),
                    "inner_1",
                    (PULL, "inner_1"),
                    result={
                        "my_key": "hi my value here",
                        "my_other_key": "hi my value",
                    },
                ),
            ),
        ),
        StateSnapshot(
            values={},
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("inner:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "input",
                "writes": {"__start__": {"my_key": "hi my value"}},
                "step": -1,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config=None,
            tasks=(
                PregelTask(
                    AnyStr(),
                    "__start__",
                    (PULL, "__start__"),
                    result={"my_key": "hi my value"},
                ),
            ),
        ),
    ]

    # resume
    app.invoke(None, config, debug=True)
    # test state w/ nested subgraph state (after resuming from interrupt)
    assert app.get_state(config) == StateSnapshot(
        values={"my_key": "hi my value here and there and back again"},
        tasks=(),
        next=(),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {
                "outer_2": {"my_key": "hi my value here and there and back again"}
            },
            "step": 3,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    # test full history at the end
    actual_history = list(app.get_state_history(config))
    expected_history = [
        StateSnapshot(
            values={"my_key": "hi my value here and there and back again"},
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {
                    "outer_2": {"my_key": "hi my value here and there and back again"}
                },
                "step": 3,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "hi my value here and there"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "outer_2",
                    (PULL, "outer_2"),
                    result={"my_key": "hi my value here and there and back again"},
                ),
            ),
            next=("outer_2",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {"inner": {"my_key": "hi my value here and there"}},
                "step": 2,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "hi my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "inner",
                    (PULL, "inner"),
                    state={
                        "configurable": {"thread_id": "1", "checkpoint_ns": AnyStr()}
                    },
                    result={"my_key": "hi my value here and there"},
                ),
            ),
            next=("inner",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {"outer_1": {"my_key": "hi my value"}},
                "step": 1,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "outer_1",
                    (PULL, "outer_1"),
                    result={"my_key": "hi my value"},
                ),
            ),
            next=("outer_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "writes": None, "step": 0},
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "__start__",
                    (PULL, "__start__"),
                    result={"my_key": "my value"},
                ),
            ),
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "writes": {"__start__": {"my_key": "my value"}},
                "step": -1,
            },
            created_at=AnyStr(),
            parent_config=None,
        ),
    ]
    assert actual_history == expected_history
    # test looking up parent state by checkpoint ID
    for actual_snapshot, expected_snapshot in zip(actual_history, expected_history):
        assert app.get_state(actual_snapshot.config) == expected_snapshot


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_doubly_nested_graph_state(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class State(TypedDict):
        my_key: str

    class ChildState(TypedDict):
        my_key: str

    class GrandChildState(TypedDict):
        my_key: str

    def grandchild_1(state: ChildState):
        return {"my_key": state["my_key"] + " here"}

    def grandchild_2(state: ChildState):
        return {
            "my_key": state["my_key"] + " and there",
        }

    grandchild = StateGraph(GrandChildState)
    grandchild.add_node("grandchild_1", grandchild_1)
    grandchild.add_node("grandchild_2", grandchild_2)
    grandchild.add_edge("grandchild_1", "grandchild_2")
    grandchild.set_entry_point("grandchild_1")
    grandchild.set_finish_point("grandchild_2")

    child = StateGraph(ChildState)
    child.add_node(
        "child_1",
        grandchild.compile(interrupt_before=["grandchild_2"]),
    )
    child.set_entry_point("child_1")
    child.set_finish_point("child_1")

    def parent_1(state: State):
        return {"my_key": "hi " + state["my_key"]}

    def parent_2(state: State):
        return {"my_key": state["my_key"] + " and back again"}

    graph = StateGraph(State)
    graph.add_node("parent_1", parent_1)
    graph.add_node("child", child.compile())
    graph.add_node("parent_2", parent_2)
    graph.set_entry_point("parent_1")
    graph.add_edge("parent_1", "child")
    graph.add_edge("child", "parent_2")
    graph.set_finish_point("parent_2")

    app = graph.compile(checkpointer=checkpointer)

    # test invoke w/ nested interrupt
    config = {"configurable": {"thread_id": "1"}}
    assert [c for c in app.stream({"my_key": "my value"}, config, subgraphs=True)] == [
        ((), {"parent_1": {"my_key": "hi my value"}}),
        (
            (AnyStr("child:"), AnyStr("child_1:")),
            {"grandchild_1": {"my_key": "hi my value here"}},
        ),
        ((), {"__interrupt__": ()}),
    ]
    # get state without subgraphs
    outer_state = app.get_state(config)
    assert outer_state == StateSnapshot(
        values={"my_key": "hi my value"},
        tasks=(
            PregelTask(
                AnyStr(),
                "child",
                (PULL, "child"),
                state={
                    "configurable": {
                        "thread_id": "1",
                        "checkpoint_ns": AnyStr("child"),
                    }
                },
            ),
        ),
        next=("child",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {"parent_1": {"my_key": "hi my value"}},
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    child_state = app.get_state(outer_state.tasks[0].state)
    assert (
        child_state.tasks[0]
        == StateSnapshot(
            values={"my_key": "hi my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "child_1",
                    (PULL, "child_1"),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr(),
                        }
                    },
                ),
            ),
            next=("child_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {"": AnyStr()},
                "source": "loop",
                "writes": None,
                "step": 0,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                }
            },
        ).tasks[0]
    )
    grandchild_state = app.get_state(child_state.tasks[0].state)
    assert grandchild_state == StateSnapshot(
        values={"my_key": "hi my value here"},
        tasks=(
            PregelTask(
                AnyStr(),
                "grandchild_2",
                (PULL, "grandchild_2"),
            ),
        ),
        next=("grandchild_2",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr(),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                        AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                    }
                ),
            }
        },
        metadata={
            "parents": AnyDict(
                {
                    "": AnyStr(),
                    AnyStr("child:"): AnyStr(),
                }
            ),
            "source": "loop",
            "writes": {"grandchild_1": {"my_key": "hi my value here"}},
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr(),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                        AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                    }
                ),
            }
        },
    )
    # get state with subgraphs
    assert app.get_state(config, subgraphs=True) == StateSnapshot(
        values={"my_key": "hi my value"},
        tasks=(
            PregelTask(
                AnyStr(),
                "child",
                (PULL, "child"),
                state=StateSnapshot(
                    values={"my_key": "hi my value"},
                    tasks=(
                        PregelTask(
                            AnyStr(),
                            "child_1",
                            (PULL, "child_1"),
                            state=StateSnapshot(
                                values={"my_key": "hi my value here"},
                                tasks=(
                                    PregelTask(
                                        AnyStr(),
                                        "grandchild_2",
                                        (PULL, "grandchild_2"),
                                    ),
                                ),
                                next=("grandchild_2",),
                                config={
                                    "configurable": {
                                        "thread_id": "1",
                                        "checkpoint_ns": AnyStr(),
                                        "checkpoint_id": AnyStr(),
                                        "checkpoint_map": AnyDict(
                                            {
                                                "": AnyStr(),
                                                AnyStr("child:"): AnyStr(),
                                                AnyStr(
                                                    re.compile(r"child:.+|child1:")
                                                ): AnyStr(),
                                            }
                                        ),
                                    }
                                },
                                metadata={
                                    "parents": AnyDict(
                                        {
                                            "": AnyStr(),
                                            AnyStr("child:"): AnyStr(),
                                        }
                                    ),
                                    "source": "loop",
                                    "writes": {
                                        "grandchild_1": {"my_key": "hi my value here"}
                                    },
                                    "step": 1,
                                },
                                created_at=AnyStr(),
                                parent_config={
                                    "configurable": {
                                        "thread_id": "1",
                                        "checkpoint_ns": AnyStr(),
                                        "checkpoint_id": AnyStr(),
                                        "checkpoint_map": AnyDict(
                                            {
                                                "": AnyStr(),
                                                AnyStr("child:"): AnyStr(),
                                                AnyStr(
                                                    re.compile(r"child:.+|child1:")
                                                ): AnyStr(),
                                            }
                                        ),
                                    }
                                },
                            ),
                        ),
                    ),
                    next=("child_1",),
                    config={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("child:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {"": AnyStr(), AnyStr("child:"): AnyStr()}
                            ),
                        }
                    },
                    metadata={
                        "parents": {"": AnyStr()},
                        "source": "loop",
                        "writes": None,
                        "step": 0,
                    },
                    created_at=AnyStr(),
                    parent_config={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("child:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {"": AnyStr(), AnyStr("child:"): AnyStr()}
                            ),
                        }
                    },
                ),
            ),
        ),
        next=("child",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {"parent_1": {"my_key": "hi my value"}},
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    # resume
    assert [c for c in app.stream(None, config, subgraphs=True)] == [
        (
            (AnyStr("child:"), AnyStr("child_1:")),
            {"grandchild_2": {"my_key": "hi my value here and there"}},
        ),
        ((AnyStr("child:"),), {"child_1": {"my_key": "hi my value here and there"}}),
        ((), {"child": {"my_key": "hi my value here and there"}}),
        ((), {"parent_2": {"my_key": "hi my value here and there and back again"}}),
    ]
    # get state with and without subgraphs
    assert (
        app.get_state(config)
        == app.get_state(config, subgraphs=True)
        == StateSnapshot(
            values={"my_key": "hi my value here and there and back again"},
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {
                    "parent_2": {"my_key": "hi my value here and there and back again"}
                },
                "step": 3,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        )
    )
    # get outer graph history
    outer_history = list(app.get_state_history(config))
    assert outer_history == [
        StateSnapshot(
            values={"my_key": "hi my value here and there and back again"},
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {
                    "parent_2": {"my_key": "hi my value here and there and back again"}
                },
                "step": 3,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "hi my value here and there"},
            next=("parent_2",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "source": "loop",
                "writes": {"child": {"my_key": "hi my value here and there"}},
                "step": 2,
                "parents": {},
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="parent_2",
                    path=(PULL, "parent_2"),
                    result={"my_key": "hi my value here and there and back again"},
                ),
            ),
        ),
        StateSnapshot(
            values={"my_key": "hi my value"},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "child",
                    (PULL, "child"),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("child"),
                        }
                    },
                    result={"my_key": "hi my value here and there"},
                ),
            ),
            next=("child",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {"parent_1": {"my_key": "hi my value"}},
                "step": 1,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"my_key": "my value"},
            next=("parent_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"source": "loop", "writes": None, "step": 0, "parents": {}},
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="parent_1",
                    path=(PULL, "parent_1"),
                    result={"my_key": "hi my value"},
                ),
            ),
        ),
        StateSnapshot(
            values={},
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "source": "input",
                "writes": {"__start__": {"my_key": "my value"}},
                "step": -1,
                "parents": {},
            },
            created_at=AnyStr(),
            parent_config=None,
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="__start__",
                    path=(PULL, "__start__"),
                    result={"my_key": "my value"},
                ),
            ),
        ),
    ]
    # get child graph history
    child_history = list(app.get_state_history(outer_history[2].tasks[0].state))
    assert child_history == [
        StateSnapshot(
            values={"my_key": "hi my value here and there"},
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": {"child_1": {"my_key": "hi my value here and there"}},
                "step": 1,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            tasks=(),
        ),
        StateSnapshot(
            values={"my_key": "hi my value"},
            next=("child_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": None,
                "step": 0,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="child_1",
                    path=(PULL, "child_1"),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("child:"),
                        }
                    },
                    result={"my_key": "hi my value here and there"},
                ),
            ),
        ),
        StateSnapshot(
            values={},
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr("child:"),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {"": AnyStr(), AnyStr("child:"): AnyStr()}
                    ),
                }
            },
            metadata={
                "source": "input",
                "writes": {"__start__": {"my_key": "hi my value"}},
                "step": -1,
                "parents": {"": AnyStr()},
            },
            created_at=AnyStr(),
            parent_config=None,
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="__start__",
                    path=(PULL, "__start__"),
                    result={"my_key": "hi my value"},
                ),
            ),
        ),
    ]
    # get grandchild graph history
    grandchild_history = list(app.get_state_history(child_history[1].tasks[0].state))
    assert grandchild_history == [
        StateSnapshot(
            values={"my_key": "hi my value here and there"},
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": {"grandchild_2": {"my_key": "hi my value here and there"}},
                "step": 2,
                "parents": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                    }
                ),
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            tasks=(),
        ),
        StateSnapshot(
            values={"my_key": "hi my value here"},
            next=("grandchild_2",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": {"grandchild_1": {"my_key": "hi my value here"}},
                "step": 1,
                "parents": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                    }
                ),
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="grandchild_2",
                    path=(PULL, "grandchild_2"),
                    result={"my_key": "hi my value here and there"},
                ),
            ),
        ),
        StateSnapshot(
            values={"my_key": "hi my value"},
            next=("grandchild_1",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            metadata={
                "source": "loop",
                "writes": None,
                "step": 0,
                "parents": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                    }
                ),
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="grandchild_1",
                    path=(PULL, "grandchild_1"),
                    result={"my_key": "hi my value here"},
                ),
            ),
        ),
        StateSnapshot(
            values={},
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": AnyStr(),
                    "checkpoint_id": AnyStr(),
                    "checkpoint_map": AnyDict(
                        {
                            "": AnyStr(),
                            AnyStr("child:"): AnyStr(),
                            AnyStr(re.compile(r"child:.+|child1:")): AnyStr(),
                        }
                    ),
                }
            },
            metadata={
                "source": "input",
                "writes": {"__start__": {"my_key": "hi my value"}},
                "step": -1,
                "parents": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("child:"): AnyStr(),
                    }
                ),
            },
            created_at=AnyStr(),
            parent_config=None,
            tasks=(
                PregelTask(
                    id=AnyStr(),
                    name="__start__",
                    path=(PULL, "__start__"),
                    result={"my_key": "hi my value"},
                ),
            ),
        ),
    ]

    # replay grandchild checkpoint
    assert [
        c for c in app.stream(None, grandchild_history[2].config, subgraphs=True)
    ] == [
        (
            (AnyStr("child:"), AnyStr("child_1:")),
            {"grandchild_1": {"my_key": "hi my value here"}},
        ),
        ((), {"__interrupt__": ()}),
    ]


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_send_to_nested_graphs(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    class OverallState(TypedDict):
        subjects: list[str]
        jokes: Annotated[list[str], operator.add]

    def continue_to_jokes(state: OverallState):
        return [Send("generate_joke", {"subject": s}) for s in state["subjects"]]

    class JokeState(TypedDict):
        subject: str

    def edit(state: JokeState):
        subject = state["subject"]
        return {"subject": f"{subject} - hohoho"}

    # subgraph
    subgraph = StateGraph(JokeState, output=OverallState)
    subgraph.add_node("edit", edit)
    subgraph.add_node(
        "generate", lambda state: {"jokes": [f"Joke about {state['subject']}"]}
    )
    subgraph.set_entry_point("edit")
    subgraph.add_edge("edit", "generate")
    subgraph.set_finish_point("generate")

    # parent graph
    builder = StateGraph(OverallState)
    builder.add_node(
        "generate_joke",
        subgraph.compile(interrupt_before=["generate"]),
    )
    builder.add_conditional_edges(START, continue_to_jokes)
    builder.add_edge("generate_joke", END)

    graph = builder.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "1"}}
    tracer = FakeTracer()

    # invoke and pause at nested interrupt
    assert graph.invoke(
        {"subjects": ["cats", "dogs"]}, config={**config, "callbacks": [tracer]}
    ) == {
        "subjects": ["cats", "dogs"],
        "jokes": [],
    }
    assert len(tracer.runs) == 1, "Should produce exactly 1 root run"

    # check state
    outer_state = graph.get_state(config)
    assert outer_state == StateSnapshot(
        values={"subjects": ["cats", "dogs"], "jokes": []},
        tasks=(
            PregelTask(
                AnyStr(),
                "generate_joke",
                (PUSH, 0),
                state={
                    "configurable": {
                        "thread_id": "1",
                        "checkpoint_ns": AnyStr("generate_joke:"),
                    }
                },
            ),
            PregelTask(
                AnyStr(),
                "generate_joke",
                (PUSH, 1),
                state={
                    "configurable": {
                        "thread_id": "1",
                        "checkpoint_ns": AnyStr("generate_joke:"),
                    }
                },
            ),
        ),
        next=("generate_joke", "generate_joke"),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={"parents": {}, "source": "loop", "writes": None, "step": 0},
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    # check state of each of the inner tasks
    assert graph.get_state(outer_state.tasks[0].state) == StateSnapshot(
        values={"subject": "cats - hohoho", "jokes": []},
        next=("generate",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr("generate_joke:"),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("generate_joke:"): AnyStr(),
                    }
                ),
            }
        },
        metadata={
            "step": 1,
            "source": "loop",
            "writes": {"edit": None},
            "parents": {"": AnyStr()},
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr("generate_joke:"),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("generate_joke:"): AnyStr(),
                    }
                ),
            }
        },
        tasks=(PregelTask(id=AnyStr(""), name="generate", path=(PULL, "generate")),),
    )
    assert graph.get_state(outer_state.tasks[1].state) == StateSnapshot(
        values={"subject": "dogs - hohoho", "jokes": []},
        next=("generate",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr("generate_joke:"),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("generate_joke:"): AnyStr(),
                    }
                ),
            }
        },
        metadata={
            "step": 1,
            "source": "loop",
            "writes": {"edit": None},
            "parents": {"": AnyStr()},
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": AnyStr("generate_joke:"),
                "checkpoint_id": AnyStr(),
                "checkpoint_map": AnyDict(
                    {
                        "": AnyStr(),
                        AnyStr("generate_joke:"): AnyStr(),
                    }
                ),
            }
        },
        tasks=(PregelTask(id=AnyStr(""), name="generate", path=(PULL, "generate")),),
    )
    # update state of dogs joke graph
    graph.update_state(outer_state.tasks[1].state, {"subject": "turtles - hohoho"})

    # continue past interrupt
    assert sorted(
        graph.stream(None, config=config), key=lambda d: d["generate_joke"]["jokes"][0]
    ) == [
        {"generate_joke": {"jokes": ["Joke about cats - hohoho"]}},
        {"generate_joke": {"jokes": ["Joke about turtles - hohoho"]}},
    ]

    actual_snapshot = graph.get_state(config)
    expected_snapshot = StateSnapshot(
        values={
            "subjects": ["cats", "dogs"],
            "jokes": ["Joke about cats - hohoho", "Joke about turtles - hohoho"],
        },
        tasks=(),
        next=(),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "parents": {},
            "source": "loop",
            "writes": {
                "generate_joke": [
                    {"jokes": ["Joke about cats - hohoho"]},
                    {"jokes": ["Joke about turtles - hohoho"]},
                ]
            },
            "step": 1,
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
    )
    assert actual_snapshot == expected_snapshot

    # test full history
    actual_history = list(graph.get_state_history(config))

    # get subgraph node state for expected history
    expected_history = [
        StateSnapshot(
            values={
                "subjects": ["cats", "dogs"],
                "jokes": ["Joke about cats - hohoho", "Joke about turtles - hohoho"],
            },
            tasks=(),
            next=(),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "loop",
                "writes": {
                    "generate_joke": [
                        {"jokes": ["Joke about cats - hohoho"]},
                        {"jokes": ["Joke about turtles - hohoho"]},
                    ]
                },
                "step": 1,
            },
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"subjects": ["cats", "dogs"], "jokes": []},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "generate_joke",
                    (PUSH, 0),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("generate_joke:"),
                        }
                    },
                    result={"jokes": ["Joke about cats - hohoho"]},
                ),
                PregelTask(
                    AnyStr(),
                    "generate_joke",
                    (PUSH, 1),
                    state={
                        "configurable": {
                            "thread_id": "1",
                            "checkpoint_ns": AnyStr("generate_joke:"),
                        }
                    },
                    result={"jokes": ["Joke about turtles - hohoho"]},
                ),
            ),
            next=("generate_joke", "generate_joke"),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={"parents": {}, "source": "loop", "writes": None, "step": 0},
            created_at=AnyStr(),
            parent_config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
        ),
        StateSnapshot(
            values={"jokes": []},
            tasks=(
                PregelTask(
                    AnyStr(),
                    "__start__",
                    (PULL, "__start__"),
                    result={"subjects": ["cats", "dogs"]},
                ),
            ),
            next=("__start__",),
            config={
                "configurable": {
                    "thread_id": "1",
                    "checkpoint_ns": "",
                    "checkpoint_id": AnyStr(),
                }
            },
            metadata={
                "parents": {},
                "source": "input",
                "writes": {"__start__": {"subjects": ["cats", "dogs"]}},
                "step": -1,
            },
            created_at=AnyStr(),
            parent_config=None,
        ),
    ]
    assert actual_history == expected_history


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_weather_subgraph(
    request: pytest.FixtureRequest, checkpointer_name: str, snapshot: SnapshotAssertion
) -> None:
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage, ToolCall
    from langchain_core.tools import tool

    from langgraph.graph import MessagesState

    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)

    # setup subgraph

    @tool
    def get_weather(city: str):
        """Get the weather for a specific city"""
        return f"I'ts sunny in {city}!"

    weather_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool_call123",
                        name="get_weather",
                        args={"city": "San Francisco"},
                    )
                ],
            )
        ]
    )

    class SubGraphState(MessagesState):
        city: str

    def model_node(state: SubGraphState, writer: StreamWriter):
        writer(" very")
        result = weather_model.invoke(state["messages"])
        return {"city": cast(AIMessage, result).tool_calls[0]["args"]["city"]}

    def weather_node(state: SubGraphState, writer: StreamWriter):
        writer(" good")
        result = get_weather.invoke({"city": state["city"]})
        return {"messages": [{"role": "assistant", "content": result}]}

    subgraph = StateGraph(SubGraphState)
    subgraph.add_node(model_node)
    subgraph.add_node(weather_node)
    subgraph.add_edge(START, "model_node")
    subgraph.add_edge("model_node", "weather_node")
    subgraph.add_edge("weather_node", END)
    subgraph = subgraph.compile(interrupt_before=["weather_node"])

    # setup main graph

    class RouterState(MessagesState):
        route: Literal["weather", "other"]

    router_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool_call123",
                        name="router",
                        args={"dest": "weather"},
                    )
                ],
            )
        ]
    )

    def router_node(state: RouterState, writer: StreamWriter):
        writer("I'm")
        system_message = "Classify the incoming query as either about weather or not."
        messages = [{"role": "system", "content": system_message}] + state["messages"]
        route = router_model.invoke(messages)
        return {"route": cast(AIMessage, route).tool_calls[0]["args"]["dest"]}

    def normal_llm_node(state: RouterState):
        return {"messages": [AIMessage("Hello!")]}

    def route_after_prediction(state: RouterState):
        if state["route"] == "weather":
            return "weather_graph"
        else:
            return "normal_llm_node"

    def weather_graph(state: RouterState):
        return subgraph.invoke(state)

    graph = StateGraph(RouterState)
    graph.add_node(router_node)
    graph.add_node(normal_llm_node)
    graph.add_node("weather_graph", weather_graph)
    graph.add_edge(START, "router_node")
    graph.add_conditional_edges("router_node", route_after_prediction)
    graph.add_edge("normal_llm_node", END)
    graph.add_edge("weather_graph", END)
    graph = graph.compile(checkpointer=checkpointer)

    assert graph.get_graph(xray=1).draw_mermaid() == snapshot

    config = {"configurable": {"thread_id": "1"}}
    thread2 = {"configurable": {"thread_id": "2"}}
    inputs = {"messages": [{"role": "user", "content": "what's the weather in sf"}]}

    # run with custom output
    assert [c for c in graph.stream(inputs, thread2, stream_mode="custom")] == [
        "I'm",
        " very",
    ]
    assert [c for c in graph.stream(None, thread2, stream_mode="custom")] == [
        " good",
    ]

    # run until interrupt
    assert [
        c
        for c in graph.stream(
            inputs, config=config, stream_mode="updates", subgraphs=True
        )
    ] == [
        ((), {"router_node": {"route": "weather"}}),
        ((AnyStr("weather_graph:"),), {"model_node": {"city": "San Francisco"}}),
        ((), {"__interrupt__": ()}),
    ]

    # check current state
    state = graph.get_state(config)
    assert state == StateSnapshot(
        values={
            "messages": [_AnyIdHumanMessage(content="what's the weather in sf")],
            "route": "weather",
        },
        next=("weather_graph",),
        config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "source": "loop",
            "writes": {"router_node": {"route": "weather"}},
            "step": 1,
            "parents": {},
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "1",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        tasks=(
            PregelTask(
                id=AnyStr(),
                name="weather_graph",
                path=(PULL, "weather_graph"),
                state={
                    "configurable": {
                        "thread_id": "1",
                        "checkpoint_ns": AnyStr("weather_graph:"),
                    }
                },
            ),
        ),
    )

    # update
    graph.update_state(state.tasks[0].state, {"city": "la"})

    # run after update
    assert [
        c
        for c in graph.stream(
            None, config=config, stream_mode="updates", subgraphs=True
        )
    ] == [
        (
            (AnyStr("weather_graph:"),),
            {
                "weather_node": {
                    "messages": [{"role": "assistant", "content": "I'ts sunny in la!"}]
                }
            },
        ),
        (
            (),
            {
                "weather_graph": {
                    "messages": [
                        _AnyIdHumanMessage(content="what's the weather in sf"),
                        _AnyIdAIMessage(content="I'ts sunny in la!"),
                    ]
                }
            },
        ),
    ]

    # try updating acting as weather node
    config = {"configurable": {"thread_id": "14"}}
    inputs = {"messages": [{"role": "user", "content": "what's the weather in sf"}]}
    assert [
        c
        for c in graph.stream(
            inputs, config=config, stream_mode="updates", subgraphs=True
        )
    ] == [
        ((), {"router_node": {"route": "weather"}}),
        ((AnyStr("weather_graph:"),), {"model_node": {"city": "San Francisco"}}),
        ((), {"__interrupt__": ()}),
    ]
    state = graph.get_state(config, subgraphs=True)
    assert state == StateSnapshot(
        values={
            "messages": [_AnyIdHumanMessage(content="what's the weather in sf")],
            "route": "weather",
        },
        next=("weather_graph",),
        config={
            "configurable": {
                "thread_id": "14",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "source": "loop",
            "writes": {"router_node": {"route": "weather"}},
            "step": 1,
            "parents": {},
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "14",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        tasks=(
            PregelTask(
                id=AnyStr(),
                name="weather_graph",
                path=(PULL, "weather_graph"),
                state=StateSnapshot(
                    values={
                        "messages": [
                            _AnyIdHumanMessage(content="what's the weather in sf")
                        ],
                        "city": "San Francisco",
                    },
                    next=("weather_node",),
                    config={
                        "configurable": {
                            "thread_id": "14",
                            "checkpoint_ns": AnyStr("weather_graph:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {
                                    "": AnyStr(),
                                    AnyStr("weather_graph:"): AnyStr(),
                                }
                            ),
                        }
                    },
                    metadata={
                        "source": "loop",
                        "writes": {"model_node": {"city": "San Francisco"}},
                        "step": 1,
                        "parents": {"": AnyStr()},
                    },
                    created_at=AnyStr(),
                    parent_config={
                        "configurable": {
                            "thread_id": "14",
                            "checkpoint_ns": AnyStr("weather_graph:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {
                                    "": AnyStr(),
                                    AnyStr("weather_graph:"): AnyStr(),
                                }
                            ),
                        }
                    },
                    tasks=(
                        PregelTask(
                            id=AnyStr(),
                            name="weather_node",
                            path=(PULL, "weather_node"),
                        ),
                    ),
                ),
            ),
        ),
    )
    graph.update_state(
        state.tasks[0].state.config,
        {"messages": [{"role": "assistant", "content": "rainy"}]},
        as_node="weather_node",
    )
    state = graph.get_state(config, subgraphs=True)
    assert state == StateSnapshot(
        values={
            "messages": [_AnyIdHumanMessage(content="what's the weather in sf")],
            "route": "weather",
        },
        next=("weather_graph",),
        config={
            "configurable": {
                "thread_id": "14",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        metadata={
            "source": "loop",
            "writes": {"router_node": {"route": "weather"}},
            "step": 1,
            "parents": {},
        },
        created_at=AnyStr(),
        parent_config={
            "configurable": {
                "thread_id": "14",
                "checkpoint_ns": "",
                "checkpoint_id": AnyStr(),
            }
        },
        tasks=(
            PregelTask(
                id=AnyStr(),
                name="weather_graph",
                path=(PULL, "weather_graph"),
                state=StateSnapshot(
                    values={
                        "messages": [
                            _AnyIdHumanMessage(content="what's the weather in sf"),
                            _AnyIdAIMessage(content="rainy"),
                        ],
                        "city": "San Francisco",
                    },
                    next=(),
                    config={
                        "configurable": {
                            "thread_id": "14",
                            "checkpoint_ns": AnyStr("weather_graph:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {
                                    "": AnyStr(),
                                    AnyStr("weather_graph:"): AnyStr(),
                                }
                            ),
                        }
                    },
                    metadata={
                        "source": "update",
                        "step": 2,
                        "writes": {
                            "weather_node": {
                                "messages": [{"role": "assistant", "content": "rainy"}]
                            }
                        },
                        "parents": {"": AnyStr()},
                    },
                    created_at=AnyStr(),
                    parent_config={
                        "configurable": {
                            "thread_id": "14",
                            "checkpoint_ns": AnyStr("weather_graph:"),
                            "checkpoint_id": AnyStr(),
                            "checkpoint_map": AnyDict(
                                {
                                    "": AnyStr(),
                                    AnyStr("weather_graph:"): AnyStr(),
                                }
                            ),
                        }
                    },
                    tasks=(),
                ),
            ),
        ),
    )
    assert [
        c
        for c in graph.stream(
            None, config=config, stream_mode="updates", subgraphs=True
        )
    ] == [
        (
            (),
            {
                "weather_graph": {
                    "messages": [
                        _AnyIdHumanMessage(content="what's the weather in sf"),
                        _AnyIdAIMessage(content="rainy"),
                    ]
                }
            },
        ),
    ]


def test_repeat_condition(snapshot: SnapshotAssertion) -> None:
    class AgentState(TypedDict):
        hello: str

    def router(state: AgentState) -> str:
        return "hmm"

    workflow = StateGraph(AgentState)
    workflow.add_node("Researcher", lambda x: x)
    workflow.add_node("Chart Generator", lambda x: x)
    workflow.add_node("Call Tool", lambda x: x)
    workflow.add_conditional_edges(
        "Researcher",
        router,
        {
            "redo": "Researcher",
            "continue": "Chart Generator",
            "call_tool": "Call Tool",
            "end": END,
        },
    )
    workflow.add_conditional_edges(
        "Chart Generator",
        router,
        {"continue": "Researcher", "call_tool": "Call Tool", "end": END},
    )
    workflow.add_conditional_edges(
        "Call Tool",
        # Each agent node updates the 'sender' field
        # the tool calling node does not, meaning
        # this edge will route back to the original agent
        # who invoked the tool
        lambda x: x["sender"],
        {
            "Researcher": "Researcher",
            "Chart Generator": "Chart Generator",
        },
    )
    workflow.set_entry_point("Researcher")

    app = workflow.compile()
    assert app.get_graph().draw_mermaid(with_styles=False) == snapshot


def test_checkpoint_metadata() -> None:
    """This test verifies that a run's configurable fields are merged with the
    previous checkpoint config for each step in the run.
    """
    # set up test
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage, AnyMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool

    # graph state
    class BaseState(TypedDict):
        messages: Annotated[list[AnyMessage], add_messages]

    # initialize graph nodes
    @tool()
    def search_api(query: str) -> str:
        """Searches the API for the query."""
        return f"result for {query}"

    tools = [search_api]

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a nice assistant."),
            ("placeholder", "{messages}"),
        ]
    )

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tool_call123",
                        "name": "search_api",
                        "args": {"query": "query"},
                    },
                ],
            ),
            AIMessage(content="answer"),
        ]
    )

    @traceable(run_type="llm")
    def agent(state: BaseState) -> BaseState:
        formatted = prompt.invoke(state)
        response = model.invoke(formatted)
        return {"messages": response, "usage_metadata": {"total_tokens": 123}}

    def should_continue(data: BaseState) -> str:
        # Logic to decide whether to continue in the loop or exit
        if not data["messages"][-1].tool_calls:
            return "exit"
        else:
            return "continue"

    # define graphs w/ and w/o interrupt
    workflow = StateGraph(BaseState)
    workflow.add_node("agent", agent)
    workflow.add_node("tools", ToolNode(tools))
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "exit": END}
    )
    workflow.add_edge("tools", "agent")

    # graph w/o interrupt
    checkpointer_1 = MemorySaverAssertCheckpointMetadata()
    app = workflow.compile(checkpointer=checkpointer_1)

    # graph w/ interrupt
    checkpointer_2 = MemorySaverAssertCheckpointMetadata()
    app_w_interrupt = workflow.compile(
        checkpointer=checkpointer_2, interrupt_before=["tools"]
    )

    # assertions

    # invoke graph w/o interrupt
    assert app.invoke(
        {"messages": ["what is weather in sf"]},
        {
            "configurable": {
                "thread_id": "1",
                "test_config_1": "foo",
                "test_config_2": "bar",
            },
        },
    ) == {
        "messages": [
            _AnyIdHumanMessage(content="what is weather in sf"),
            _AnyIdAIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_api",
                        "args": {"query": "query"},
                        "id": "tool_call123",
                        "type": "tool_call",
                    }
                ],
            ),
            _AnyIdToolMessage(
                content="result for query",
                name="search_api",
                tool_call_id="tool_call123",
            ),
            _AnyIdAIMessage(content="answer"),
        ]
    }

    config = {"configurable": {"thread_id": "1"}}

    # assert that checkpoint metadata contains the run's configurable fields
    chkpnt_metadata_1 = checkpointer_1.get_tuple(config).metadata
    assert chkpnt_metadata_1["thread_id"] == "1"
    assert chkpnt_metadata_1["test_config_1"] == "foo"
    assert chkpnt_metadata_1["test_config_2"] == "bar"

    # Verify that all checkpoint metadata have the expected keys. This check
    # is needed because a run may have an arbitrary number of steps depending
    # on how the graph is constructed.
    chkpnt_tuples_1 = checkpointer_1.list(config)
    for chkpnt_tuple in chkpnt_tuples_1:
        assert chkpnt_tuple.metadata["thread_id"] == "1"
        assert chkpnt_tuple.metadata["test_config_1"] == "foo"
        assert chkpnt_tuple.metadata["test_config_2"] == "bar"

    # invoke graph, but interrupt before tool call
    app_w_interrupt.invoke(
        {"messages": ["what is weather in sf"]},
        {
            "configurable": {
                "thread_id": "2",
                "test_config_3": "foo",
                "test_config_4": "bar",
            },
        },
    )

    config = {"configurable": {"thread_id": "2"}}

    # assert that checkpoint metadata contains the run's configurable fields
    chkpnt_metadata_2 = checkpointer_2.get_tuple(config).metadata
    assert chkpnt_metadata_2["thread_id"] == "2"
    assert chkpnt_metadata_2["test_config_3"] == "foo"
    assert chkpnt_metadata_2["test_config_4"] == "bar"

    # resume graph execution
    app_w_interrupt.invoke(
        input=None,
        config={
            "configurable": {
                "thread_id": "2",
                "test_config_3": "foo",
                "test_config_4": "bar",
            }
        },
    )

    # assert that checkpoint metadata contains the run's configurable fields
    chkpnt_metadata_3 = checkpointer_2.get_tuple(config).metadata
    assert chkpnt_metadata_3["thread_id"] == "2"
    assert chkpnt_metadata_3["test_config_3"] == "foo"
    assert chkpnt_metadata_3["test_config_4"] == "bar"

    # Verify that all checkpoint metadata have the expected keys. This check
    # is needed because a run may have an arbitrary number of steps depending
    # on how the graph is constructed.
    chkpnt_tuples_2 = checkpointer_2.list(config)
    for chkpnt_tuple in chkpnt_tuples_2:
        assert chkpnt_tuple.metadata["thread_id"] == "2"
        assert chkpnt_tuple.metadata["test_config_3"] == "foo"
        assert chkpnt_tuple.metadata["test_config_4"] == "bar"


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_remove_message_via_state_update(
    request: pytest.FixtureRequest, checkpointer_name: str
) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

    workflow = MessageGraph()
    workflow.add_node(
        "chatbot",
        lambda state: [
            AIMessage(
                content="Hello! How can I help you",
            )
        ],
    )

    workflow.set_entry_point("chatbot")
    workflow.add_edge("chatbot", END)

    checkpointer = request.getfixturevalue("checkpointer_" + checkpointer_name)
    app = workflow.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "1"}}
    output = app.invoke([HumanMessage(content="Hi")], config=config)
    app.update_state(config, values=[RemoveMessage(id=output[-1].id)])

    updated_state = app.get_state(config)

    assert len(updated_state.values) == 1
    assert updated_state.values[-1].content == "Hi"


def test_remove_message_from_node():
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

    workflow = MessageGraph()
    workflow.add_node(
        "chatbot",
        lambda state: [
            AIMessage(
                content="Hello!",
            ),
            AIMessage(
                content="How can I help you?",
            ),
        ],
    )
    workflow.add_node("delete_messages", lambda state: [RemoveMessage(id=state[-2].id)])
    workflow.set_entry_point("chatbot")
    workflow.add_edge("chatbot", "delete_messages")
    workflow.add_edge("delete_messages", END)

    app = workflow.compile()
    output = app.invoke([HumanMessage(content="Hi")])
    assert len(output) == 2
    assert output[-1].content == "How can I help you?"


def test_xray_lance(snapshot: SnapshotAssertion):
    from langchain_core.messages import AnyMessage, HumanMessage
    from pydantic import BaseModel, Field

    class Analyst(BaseModel):
        affiliation: str = Field(
            description="Primary affiliation of the investment analyst.",
        )
        name: str = Field(
            description="Name of the investment analyst.",
            pattern=r"^[a-zA-Z0-9_-]{1,64}$",
        )
        role: str = Field(
            description="Role of the investment analyst in the context of the topic.",
        )
        description: str = Field(
            description="Description of the investment analyst focus, concerns, and motives.",
        )

        @property
        def persona(self) -> str:
            return f"Name: {self.name}\nRole: {self.role}\nAffiliation: {self.affiliation}\nDescription: {self.description}\n"

    class Perspectives(BaseModel):
        analysts: List[Analyst] = Field(
            description="Comprehensive list of investment analysts with their roles and affiliations.",
        )

    class Section(BaseModel):
        section_title: str = Field(..., title="Title of the section")
        context: str = Field(
            ..., title="Provide a clear summary of the focus area that you researched."
        )
        findings: str = Field(
            ...,
            title="Give a clear and detailed overview of your findings based upon the expert interview.",
        )
        thesis: str = Field(
            ...,
            title="Give a clear and specific investment thesis based upon these findings.",
        )

    class InterviewState(TypedDict):
        messages: Annotated[List[AnyMessage], add_messages]
        analyst: Analyst
        section: Section

    class ResearchGraphState(TypedDict):
        analysts: List[Analyst]
        topic: str
        max_analysts: int
        sections: List[Section]
        interviews: Annotated[list, operator.add]

    # Conditional edge
    def route_messages(state):
        return "ask_question"

    def generate_question(state):
        return ...

    def generate_answer(state):
        return ...

    # Add nodes and edges
    interview_builder = StateGraph(InterviewState)
    interview_builder.add_node("ask_question", generate_question)
    interview_builder.add_node("answer_question", generate_answer)

    # Flow
    interview_builder.add_edge(START, "ask_question")
    interview_builder.add_edge("ask_question", "answer_question")
    interview_builder.add_conditional_edges("answer_question", route_messages)

    # Set up memory
    memory = MemorySaver()

    # Interview
    interview_graph = interview_builder.compile(checkpointer=memory).with_config(
        run_name="Conduct Interviews"
    )

    # View
    assert interview_graph.get_graph().to_json() == snapshot

    def run_all_interviews(state: ResearchGraphState):
        """Edge to run the interview sub-graph using Send"""
        return [
            Send(
                "conduct_interview",
                {
                    "analyst": Analyst(),
                    "messages": [
                        HumanMessage(
                            content="So you said you were writing an article on ...?"
                        )
                    ],
                },
            )
            for s in state["analysts"]
        ]

    def generate_sections(state: ResearchGraphState):
        return ...

    def generate_analysts(state: ResearchGraphState):
        return ...

    builder = StateGraph(ResearchGraphState)
    builder.add_node("generate_analysts", generate_analysts)
    builder.add_node("conduct_interview", interview_builder.compile())
    builder.add_node("generate_sections", generate_sections)

    builder.add_edge(START, "generate_analysts")
    builder.add_conditional_edges(
        "generate_analysts", run_all_interviews, ["conduct_interview"]
    )
    builder.add_edge("conduct_interview", "generate_sections")
    builder.add_edge("generate_sections", END)

    graph = builder.compile()

    # View
    assert graph.get_graph().to_json() == snapshot
    assert graph.get_graph(xray=1).to_json() == snapshot


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
def test_channel_values(request: pytest.FixtureRequest, checkpointer_name: str) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")

    config = {"configurable": {"thread_id": "1"}}
    chain = Channel.subscribe_to("input") | Channel.write_to("output")
    app = Pregel(
        nodes={
            "one": chain,
        },
        channels={
            "ephemeral": EphemeralValue(Any),
            "input": LastValue(int),
            "output": LastValue(int),
        },
        input_channels=["input", "ephemeral"],
        output_channels="output",
        checkpointer=checkpointer,
    )
    app.invoke({"input": 1, "ephemeral": "meow"}, config)
    assert checkpointer.get(config)["channel_values"] == {"input": 1, "output": 1}


def test_xray_issue(snapshot: SnapshotAssertion) -> None:
    class State(TypedDict):
        messages: Annotated[list, add_messages]

    def node(name):
        def _node(state: State):
            return {"messages": [("human", f"entered {name} node")]}

        return _node

    parent = StateGraph(State)
    child = StateGraph(State)

    child.add_node("c_one", node("c_one"))
    child.add_node("c_two", node("c_two"))

    child.add_edge("__start__", "c_one")
    child.add_edge("c_two", "c_one")

    child.add_conditional_edges(
        "c_one", lambda x: str(randrange(0, 2)), {"0": "c_two", "1": "__end__"}
    )

    parent.add_node("p_one", node("p_one"))
    parent.add_node("p_two", child.compile())

    parent.add_edge("__start__", "p_one")
    parent.add_edge("p_two", "p_one")

    parent.add_conditional_edges(
        "p_one", lambda x: str(randrange(0, 2)), {"0": "p_two", "1": "__end__"}
    )

    app = parent.compile()

    assert app.get_graph(xray=True).draw_mermaid() == snapshot


def test_xray_bool(snapshot: SnapshotAssertion) -> None:
    class State(TypedDict):
        messages: Annotated[list, add_messages]

    def node(name):
        def _node(state: State):
            return {"messages": [("human", f"entered {name} node")]}

        return _node

    grand_parent = StateGraph(State)

    child = StateGraph(State)

    child.add_node("c_one", node("c_one"))
    child.add_node("c_two", node("c_two"))

    child.add_edge("__start__", "c_one")
    child.add_edge("c_two", "c_one")

    child.add_conditional_edges(
        "c_one", lambda x: str(randrange(0, 2)), {"0": "c_two", "1": "__end__"}
    )

    parent = StateGraph(State)
    parent.add_node("p_one", node("p_one"))
    parent.add_node("p_two", child.compile())
    parent.add_edge("__start__", "p_one")
    parent.add_edge("p_two", "p_one")
    parent.add_conditional_edges(
        "p_one", lambda x: str(randrange(0, 2)), {"0": "p_two", "1": "__end__"}
    )

    grand_parent.add_node("gp_one", node("gp_one"))
    grand_parent.add_node("gp_two", parent.compile())
    grand_parent.add_edge("__start__", "gp_one")
    grand_parent.add_edge("gp_two", "gp_one")
    grand_parent.add_conditional_edges(
        "gp_one", lambda x: str(randrange(0, 2)), {"0": "gp_two", "1": "__end__"}
    )

    app = grand_parent.compile()
    assert app.get_graph(xray=True).draw_mermaid() == snapshot


def test_multiple_sinks_subgraphs(snapshot: SnapshotAssertion) -> None:
    class State(TypedDict):
        messages: Annotated[list, add_messages]

    subgraph_builder = StateGraph(State)
    subgraph_builder.add_node("one", lambda x: x)
    subgraph_builder.add_node("two", lambda x: x)
    subgraph_builder.add_node("three", lambda x: x)
    subgraph_builder.add_edge("__start__", "one")
    subgraph_builder.add_conditional_edges("one", lambda x: "two", ["two", "three"])
    subgraph = subgraph_builder.compile()

    builder = StateGraph(State)
    builder.add_node("uno", lambda x: x)
    builder.add_node("dos", lambda x: x)
    builder.add_node("subgraph", subgraph)
    builder.add_edge("__start__", "uno")
    builder.add_conditional_edges("uno", lambda x: "dos", ["dos", "subgraph"])

    app = builder.compile()
    assert app.get_graph(xray=True).draw_mermaid() == snapshot


def test_subgraph_retries():
    class State(TypedDict):
        count: int

    class ChildState(State):
        some_list: Annotated[list, operator.add]

    called_times = 0

    class RandomError(ValueError):
        """This will be retried on."""

    def parent_node(state: State):
        return {"count": state["count"] + 1}

    def child_node_a(state: ChildState):
        nonlocal called_times
        # We want it to retry only on node_b
        # NOT re-compute the whole graph.
        assert not called_times
        called_times += 1
        return {"some_list": ["val"]}

    def child_node_b(state: ChildState):
        raise RandomError("First attempt fails")

    child = StateGraph(ChildState)
    child.add_node(child_node_a)
    child.add_node(child_node_b)
    child.add_edge("__start__", "child_node_a")
    child.add_edge("child_node_a", "child_node_b")

    parent = StateGraph(State)
    parent.add_node("parent_node", parent_node)
    parent.add_node(
        "child_graph",
        child.compile(),
        retry=RetryPolicy(
            max_attempts=3,
            retry_on=(RandomError,),
            backoff_factor=0.0001,
            initial_interval=0.0001,
        ),
    )
    parent.add_edge("parent_node", "child_graph")
    parent.set_entry_point("parent_node")

    checkpointer = MemorySaver()
    app = parent.compile(checkpointer=checkpointer)
    with pytest.raises(RandomError):
        app.invoke({"count": 0}, {"configurable": {"thread_id": "foo"}})


@pytest.mark.parametrize("checkpointer_name", ALL_CHECKPOINTERS_SYNC)
@pytest.mark.parametrize("store_name", ALL_STORES_SYNC)
def test_store_injected(
    request: pytest.FixtureRequest, checkpointer_name: str, store_name: str
) -> None:
    checkpointer = request.getfixturevalue(f"checkpointer_{checkpointer_name}")
    the_store = request.getfixturevalue(f"store_{store_name}")

    class State(TypedDict):
        count: Annotated[int, operator.add]

    doc_id = str(uuid.uuid4())
    doc = {"some-key": "this-is-a-val"}

    def node(input: State, config: RunnableConfig, store: BaseStore):
        assert isinstance(store, BaseStore)
        store.put(
            ("foo", "bar"),
            doc_id,
            {
                **doc,
                "from_thread": config["configurable"]["thread_id"],
                "some_val": input["count"],
            },
        )
        return {"count": 1}

    builder = StateGraph(State)
    builder.add_node("node", node)
    builder.add_edge("__start__", "node")
    graph = builder.compile(store=the_store, checkpointer=checkpointer)

    thread_1 = str(uuid.uuid4())
    result = graph.invoke({"count": 0}, {"configurable": {"thread_id": thread_1}})
    assert result == {"count": 1}
    returned_doc = the_store.get(("foo", "bar"), doc_id).value
    assert returned_doc == {**doc, "from_thread": thread_1, "some_val": 0}
    assert len(the_store.search(("foo", "bar"))) == 1

    # Check update on existing thread
    result = graph.invoke({"count": 0}, {"configurable": {"thread_id": thread_1}})
    assert result == {"count": 2}
    returned_doc = the_store.get(("foo", "bar"), doc_id).value
    assert returned_doc == {**doc, "from_thread": thread_1, "some_val": 1}
    assert len(the_store.search(("foo", "bar"))) == 1

    thread_2 = str(uuid.uuid4())

    result = graph.invoke({"count": 0}, {"configurable": {"thread_id": thread_2}})
    assert result == {"count": 1}
    returned_doc = the_store.get(("foo", "bar"), doc_id).value
    assert returned_doc == {
        **doc,
        "from_thread": thread_2,
        "some_val": 0,
    }  # Overwrites the whole doc
    assert len(the_store.search(("foo", "bar"))) == 1  # still overwriting the same one


def test_enum_node_names():
    class NodeName(str, enum.Enum):
        BAZ = "baz"

    class State(TypedDict):
        foo: str
        bar: str

    def baz(state: State):
        return {"bar": state["foo"] + "!"}

    graph = StateGraph(State)
    graph.add_node(NodeName.BAZ, baz)
    graph.add_edge(START, NodeName.BAZ)
    graph.add_edge(NodeName.BAZ, END)
    graph = graph.compile()

    assert graph.invoke({"foo": "hello"}) == {"foo": "hello", "bar": "hello!"}


def test_debug_retry():
    class State(TypedDict):
        messages: Annotated[list[str], operator.add]

    def node(name):
        def _node(state: State):
            return {"messages": [f"entered {name} node"]}

        return _node

    builder = StateGraph(State)
    builder.add_node("one", node("one"))
    builder.add_node("two", node("two"))
    builder.add_edge(START, "one")
    builder.add_edge("one", "two")
    builder.add_edge("two", END)

    saver = MemorySaver()

    graph = builder.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": "1"}}
    graph.invoke({"messages": []}, config=config)

    # re-run step: 1
    target_config = next(
        c.parent_config for c in saver.list(config) if c.metadata["step"] == 1
    )
    update_config = graph.update_state(target_config, values=None)

    events = [*graph.stream(None, config=update_config, stream_mode="debug")]

    checkpoint_events = list(
        reversed([e["payload"] for e in events if e["type"] == "checkpoint"])
    )

    checkpoint_history = {
        c.config["configurable"]["checkpoint_id"]: c
        for c in graph.get_state_history(config)
    }

    def lax_normalize_config(config: Optional[dict]) -> Optional[dict]:
        if config is None:
            return None
        return config["configurable"]

    for stream in checkpoint_events:
        stream_conf = lax_normalize_config(stream["config"])
        stream_parent_conf = lax_normalize_config(stream["parent_config"])
        assert stream_conf != stream_parent_conf

        # ensure the streamed checkpoint == checkpoint from checkpointer.list()
        history = checkpoint_history[stream["config"]["configurable"]["checkpoint_id"]]
        history_conf = lax_normalize_config(history.config)
        assert stream_conf == history_conf

        history_parent_conf = lax_normalize_config(history.parent_config)
        assert stream_parent_conf == history_parent_conf


def test_debug_subgraphs():
    class State(TypedDict):
        messages: Annotated[list[str], operator.add]

    def node(name):
        def _node(state: State):
            return {"messages": [f"entered {name} node"]}

        return _node

    parent = StateGraph(State)
    child = StateGraph(State)

    child.add_node("c_one", node("c_one"))
    child.add_node("c_two", node("c_two"))
    child.add_edge(START, "c_one")
    child.add_edge("c_one", "c_two")
    child.add_edge("c_two", END)

    parent.add_node("p_one", node("p_one"))
    parent.add_node("p_two", child.compile())
    parent.add_edge(START, "p_one")
    parent.add_edge("p_one", "p_two")
    parent.add_edge("p_two", END)

    graph = parent.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "1"}}
    events = [
        *graph.stream(
            {"messages": []},
            config=config,
            stream_mode="debug",
        )
    ]

    checkpoint_events = list(
        reversed([e["payload"] for e in events if e["type"] == "checkpoint"])
    )
    checkpoint_history = list(graph.get_state_history(config))

    assert len(checkpoint_events) == len(checkpoint_history)

    def lax_normalize_config(config: Optional[dict]) -> Optional[dict]:
        if config is None:
            return None
        return config["configurable"]

    for stream, history in zip(checkpoint_events, checkpoint_history):
        assert stream["values"] == history.values
        assert stream["next"] == list(history.next)
        assert lax_normalize_config(stream["config"]) == lax_normalize_config(
            history.config
        )
        assert lax_normalize_config(stream["parent_config"]) == lax_normalize_config(
            history.parent_config
        )

        assert len(stream["tasks"]) == len(history.tasks)
        for stream_task, history_task in zip(stream["tasks"], history.tasks):
            assert stream_task["id"] == history_task.id
            assert stream_task["name"] == history_task.name
            assert stream_task["interrupts"] == history_task.interrupts
            assert stream_task.get("error") == history_task.error
            assert stream_task.get("state") == history_task.state


def test_debug_nested_subgraphs():
    from collections import defaultdict

    class State(TypedDict):
        messages: Annotated[list[str], operator.add]

    def node(name):
        def _node(state: State):
            return {"messages": [f"entered {name} node"]}

        return _node

    grand_parent = StateGraph(State)
    parent = StateGraph(State)
    child = StateGraph(State)

    child.add_node("c_one", node("c_one"))
    child.add_node("c_two", node("c_two"))
    child.add_edge(START, "c_one")
    child.add_edge("c_one", "c_two")
    child.add_edge("c_two", END)

    parent.add_node("p_one", node("p_one"))
    parent.add_node("p_two", child.compile())
    parent.add_edge(START, "p_one")
    parent.add_edge("p_one", "p_two")
    parent.add_edge("p_two", END)

    grand_parent.add_node("gp_one", node("gp_one"))
    grand_parent.add_node("gp_two", parent.compile())
    grand_parent.add_edge(START, "gp_one")
    grand_parent.add_edge("gp_one", "gp_two")
    grand_parent.add_edge("gp_two", END)

    graph = grand_parent.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "1"}}
    events = [
        *graph.stream(
            {"messages": []},
            config=config,
            stream_mode="debug",
            subgraphs=True,
        )
    ]

    stream_ns: dict[tuple, dict] = defaultdict(list)
    for ns, e in events:
        if e["type"] == "checkpoint":
            stream_ns[ns].append(e["payload"])

    assert list(stream_ns.keys()) == [
        (),
        (AnyStr("gp_two:"),),
        (AnyStr("gp_two:"), AnyStr("p_two:")),
    ]

    history_ns = {
        ns: list(
            graph.get_state_history(
                {"configurable": {"thread_id": "1", "checkpoint_ns": "|".join(ns)}}
            )
        )[::-1]
        for ns in stream_ns.keys()
    }

    def normalize_config(config: Optional[dict]) -> Optional[dict]:
        if config is None:
            return None

        clean_config = {}
        clean_config["thread_id"] = config["configurable"]["thread_id"]
        clean_config["checkpoint_id"] = config["configurable"]["checkpoint_id"]
        clean_config["checkpoint_ns"] = config["configurable"]["checkpoint_ns"]
        if "checkpoint_map" in config["configurable"]:
            clean_config["checkpoint_map"] = config["configurable"]["checkpoint_map"]

        return clean_config

    for checkpoint_events, checkpoint_history in zip(
        stream_ns.values(), history_ns.values()
    ):
        for stream, history in zip(checkpoint_events, checkpoint_history):
            assert stream["values"] == history.values
            assert stream["next"] == list(history.next)
            assert normalize_config(stream["config"]) == normalize_config(
                history.config
            )
            assert normalize_config(stream["parent_config"]) == normalize_config(
                history.parent_config
            )

            assert len(stream["tasks"]) == len(history.tasks)
            for stream_task, history_task in zip(stream["tasks"], history.tasks):
                assert stream_task["id"] == history_task.id
                assert stream_task["name"] == history_task.name
                assert stream_task["interrupts"] == history_task.interrupts
                assert stream_task.get("error") == history_task.error
                assert stream_task.get("state") == history_task.state
