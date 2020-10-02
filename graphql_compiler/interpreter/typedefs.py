from abc import ABCMeta, abstractmethod
from pprint import pformat
from typing import (
    AbstractSet,
    Any,
    Collection,
    Dict,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
)

from ..compiler.helpers import Location
from ..compiler.metadata import FilterInfo
from ..typedefs import Literal, TypedDict
from .immutable_stack import ImmutableStack, make_empty_stack


GLOBAL_LOCATION_TYPE_NAME = "__global__"


DataToken = TypeVar("DataToken")


class DataContext(Generic[DataToken]):

    __slots__ = (
        "current_token",
        "token_at_location",
        "expression_stack",
        "piggyback_contexts",
    )

    current_token: Optional[DataToken]
    token_at_location: Dict[Location, Optional[DataToken]]
    expression_stack: ImmutableStack
    piggyback_contexts: Optional[List["DataContext"]]

    def __init__(
        self,
        current_token: Optional[DataToken],
        token_at_location: Dict[Location, Optional[DataToken]],
        expression_stack: ImmutableStack,
    ) -> None:
        self.current_token = current_token
        self.token_at_location = token_at_location
        self.expression_stack = expression_stack
        self.piggyback_contexts = None

    def __repr__(self) -> str:
        return (
            f"DataContext(current={self.current_token}, "
            f"locations={pformat(self.token_at_location)}, "
            f"stack={pformat(self.expression_stack)}, "
            f"piggyback={self.piggyback_contexts})"
        )

    __str__ = __repr__

    @staticmethod
    def make_empty_context_from_token(token: DataToken) -> "DataContext":
        return DataContext(token, dict(), make_empty_stack())

    def push_value_onto_stack(self, value: Any) -> "DataContext":
        self.expression_stack = self.expression_stack.push(value)
        return self  # for chaining

    def peek_value_on_stack(self) -> Any:
        return self.expression_stack.value

    def pop_value_from_stack(self) -> Any:
        value, remaining_stack = self.expression_stack.pop()
        if remaining_stack is None:
            raise AssertionError(
                'We always start the stack with a "None" element pushed on, but '
                "that element somehow got popped off. This is a bug."
            )
        self.expression_stack = remaining_stack
        return value

    def get_context_for_location(self, location: Location) -> "DataContext":
        return DataContext(
            self.token_at_location[location],
            dict(self.token_at_location),
            self.expression_stack,
        )

    def add_piggyback_context(self, piggyback: "DataContext") -> None:
        # First, move any nested piggyback contexts to this context's piggyback list
        nested_piggyback_contexts = piggyback.consume_piggyback_contexts()

        if self.piggyback_contexts:
            self.piggyback_contexts.extend(nested_piggyback_contexts)
        else:
            self.piggyback_contexts = nested_piggyback_contexts

        # Then, append the new piggyback element to our own piggyback contexts.
        self.piggyback_contexts.append(piggyback)

    def consume_piggyback_contexts(self) -> List["DataContext"]:
        piggybacks = self.piggyback_contexts
        if piggybacks is None:
            return []

        self.piggyback_contexts = None
        return piggybacks

    def ensure_deactivated(self) -> None:
        if self.current_token is not None:
            self.push_value_onto_stack(self.current_token)
            self.current_token = None

    def reactivate(self) -> None:
        if self.current_token is not None:
            raise AssertionError(f"Attempting to reactivate an already-active context: {self}")
        self.current_token = self.pop_value_from_stack()


EdgeDirection = Literal["in", "out"]
EdgeInfo = Tuple[EdgeDirection, str]  # direction + edge name

# TODO(predrag): Figure out a better type here. We need to balance between finding something
#                easy and lightweight, and letting the user know about things like:
#                optional edges, recursive edges, used fields/filters at the neighbor, etc.
#                Will probably punt on this until the API is stabilized, since defining something
#                here is not a breaking change.
NeighborHint = Any


class InterpreterHints(TypedDict):
    """Describe all known hint types.

    Values of this type are intended to be used as "**hints" syntax in adapter calls.
    """

    runtime_arg_hints: Mapping[str, Any]  # the runtime arguments passed for this query
    used_property_hints: AbstractSet[str]  # the names of all property fields used within this scope
    filter_hints: Collection[FilterInfo]  # info on all filters used within this scope
    neighbor_hints: Collection[Tuple[EdgeInfo, NeighborHint]]  # info on all neighbors of this scope


class InterpreterAdapter(Generic[DataToken], metaclass=ABCMeta):
    """Base class defining the API for schema-aware interpreter functionality over some schema.

    This ABC is the abstraction through which the rest of the interpreter is schema-agnostic:
    the rest of the interpreter code simply takes an instance of InterpreterAdapter and performs
    all schema-aware operations through its simple, four-method API.

    ## The DataToken type parameter

    This class is generic on an implementer-chosen DataToken type, which to the rest of the library
    represents an opaque reference to the data contained by a particular vertex in the data set
    described by your chosen schema. For example, if building a subclass of InterpreterAdapter
    called MyAdapter with dict as the DataToken type, MyAdapter should be defined as follows:

        class MyAdapter(InterpreterAdapter[dict]):
            ...

    Here are a few common examples of DataToken types in practice:
    - a dict containing the type name of the vertex and the values of all its properties;
    - a dataclass containing the type name of the vertex, and the collection name and primary key
      of the database entry using which the property values can be looked up, or
    - an instance of a custom class which has *some* of the values of the vertex properties, and
      has sufficient information to look up the rest of them if they are ever requested.

    The best choice of DataToken type is dependent on the specific use case, e.g. whether the data
    is already available in Python memory, or is on a local disk, or is a network hop away.

    Implementers are free to choose any DataToken type and the interpreter code will happily use it.
    However, for the sake of easier debugging and testing using the built-in functionality in
    this library, it is desirable to make DataToken be a deep-copyable type that implements
    equality beyond a simple referential equality check.

    ## The InterpreterAdapter API

    The methods in the InterpreterAdapter API are all designed to support generator-style operation,
    where data is produced and consumed only when required. Here is a high-level description of
    the methods in the InterpreterAdapter API:
    - get_tokens_of_type() produces an iterable of DataTokens of the type specified by its argument.
      The calling function will wrap the DataTokens into a bookkeeping object called a DataContext,
      where a particular token is currently active and specified in the "current_token" attribute.
    - For an iterable of such DataContexts, project_property() can be used to get the value
      of one of the properties on the vertex type represented by the currently active DataToken
      in each DataContext; project_property() therefore returns an iterable of
      tuples (data_context, value).
    - project_neighbors() is similar: for an iterable of DataContexts and a specific edge name,
      it returns an iterable (data_context, iterable_of_neighbor_tokens) where each result
      contains the DataTokens of the neighboring vertices along that edge for the vertex whose
      DataToken is currently active in that DataContext.
    - can_coerce_to_type() is used to check whether a DataToken corresponding to one vertex type
      can be safely converted into one representing a different vertex type. Given an iterable of
      DataContexts and the name of the type to which the conversion is attempted, it produces
      an iterable of tuples (data_context, can_coerce) where can_coerce is a boolean.

    ## Performance and optimization opportunities

    The design of the API, including its generator-style operation, enable a variety of
    optimizations to either happen automatically or be available with minimal additional work.
    A few simple examples:
    - Interpreters perform lazy evaluation by default: if exactly 3 query results are requested,
      then only the minimal data necessary to produce *exactly 3 rows' worth* of outputs is loaded.
    - When computing a particular result, data loading for output fields is deferred
      until *after* all filtering operations have been completed, to minimize data loads.
    - Data caching is easy to implement within this API -- simply have
      your API function's implementation consult a cache before performing the requested operation.
    - Batch-loading of data can be performed by simply advancing the input generator multiple times,
      then operating on an entire batch of input data before producing corresponding outputs:

        def project_property(
            self,
            data_contexts: Iterable[DataContext[DataToken]],
            current_type_name: str,
            field_name: str,
            **hints: Any
        ) -> Iterable[Tuple[DataContext[DataToken], Any]]:
            for data_context_batch in funcy.chunks(30, data_contexts):
                # Data for 30 entries is now in data_context_batch, operate on it in bulk.
                results_batch = compute_results_for_batch(
                    data_context_batch, current_type_name, field_name
                )
                yield from results_batch

    Additionally, each of the four methods in the API takes several kwargs whose names
    end with the suffix "_hints", in addition to the catch-all "**hints: Any" argument. These
    provide each function with information about how the data it is currently processing will
    be used in subsequent operations, and can therefore enable additional interesting optimizations.
    Use of these hints is totally optional (the library always assumes that the hints weren't used),
    so subclasses of InterpreterAdapter may even safely ignore these kwargs entirely -- for example,
    if the "runtime_arg_hints" kwarg is omitted in the method definition, at call time its value
    will go into the catch-all "**hints" argument instead.

    The set of hints (and the information each hint provides) could grow in the future. Currently,
    the following hints are offered:
    - runtime_arg_hints: the values of any runtime arguments provided to the query for use in
      filtering operations (recall queries' "$foo" filter parameter syntax).
    - used_property_hints: the property names within the scope relevant to the called function that
      the query will eventually need, e.g. for filtering on, or to output as the final result.
    - filter_hints: information about the filters applied within the scope relevant to the called
      function, such as "which filtering operation is being performed?" and "with which arguments?"
    - neighbor_hints: information about the edges originating from the scope relevant to the called
      function that the query will eventually need to expand.

    More details on these hints, and suggestions for their use, can be found in the API methods
    docstrings available below.
    """

    @abstractmethod
    def get_tokens_of_type(
        self,
        type_name: str,
        *,
        runtime_arg_hints: Optional[Mapping[str, Any]] = None,
        used_property_hints: Optional[AbstractSet[str]] = None,
        filter_hints: Optional[Collection[FilterInfo]] = None,
        neighbor_hints: Optional[Collection[Tuple[EdgeInfo, NeighborHint]]] = None,
        **hints: Any,
    ) -> Iterable[DataToken]:
        """Produce an iterable of tokens for the specified type name."""
        # TODO(predrag): Add more docs in an upcoming PR.
        pass

    @abstractmethod
    def project_property(
        self,
        data_contexts: Iterable[DataContext[DataToken]],
        current_type_name: str,
        field_name: str,
        *,
        runtime_arg_hints: Optional[Mapping[str, Any]] = None,
        used_property_hints: Optional[AbstractSet[str]] = None,
        filter_hints: Optional[Collection[FilterInfo]] = None,
        neighbor_hints: Optional[Collection[Tuple[EdgeInfo, NeighborHint]]] = None,
        **hints: Any,
    ) -> Iterable[Tuple[DataContext[DataToken], Any]]:
        """Produce the values for a given property for each of an iterable of input DataTokens."""
        # TODO(predrag): Add more docs in an upcoming PR.

    @abstractmethod
    def project_neighbors(
        self,
        data_contexts: Iterable[DataContext[DataToken]],
        current_type_name: str,
        edge_info: EdgeInfo,
        *,
        runtime_arg_hints: Optional[Mapping[str, Any]] = None,
        used_property_hints: Optional[AbstractSet[str]] = None,
        filter_hints: Optional[Collection[FilterInfo]] = None,
        neighbor_hints: Optional[Collection[Tuple[EdgeInfo, NeighborHint]]] = None,
        **hints: Any,
    ) -> Iterable[Tuple[DataContext[DataToken], Iterable[DataToken]]]:
        """Produce the neighbors along a given edge for each of an iterable of input DataTokens."""
        # TODO(predrag): Add more docs in an upcoming PR.
        #
        # If using a generator instead of a list for the Iterable[DataToken] part,
        # be careful -- generators are not closures! Make sure any state you pull into
        # the generator from the outside does not change, or that bug will be hard to find.
        # Remember: it's always safer to use a function to produce the generator, since
        # that will explicitly preserve all the external values passed into it.
        pass

    @abstractmethod
    def can_coerce_to_type(
        self,
        data_contexts: Iterable[DataContext[DataToken]],
        current_type_name: str,
        coerce_to_type_name: str,
        *,
        runtime_arg_hints: Optional[Mapping[str, Any]] = None,
        used_property_hints: Optional[AbstractSet[str]] = None,
        filter_hints: Optional[Collection[FilterInfo]] = None,
        neighbor_hints: Optional[Collection[Tuple[EdgeInfo, NeighborHint]]] = None,
        **hints: Any,
    ) -> Iterable[Tuple[DataContext[DataToken], bool]]:
        """Determine if each of an iterable of input DataTokens can be coerced to another type."""
        # TODO(predrag): Add more docs in an upcoming PR.
        pass