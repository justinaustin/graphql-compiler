# Copyright 2019-present Kensho Technologies, LLC.
"""Implement renaming and suppressing parts of a GraphQL schema.

There are two ways to rename a part of a schema: 1-1 renaming and 1-many renaming.

1-1 renaming replaces the name of a type, field, or enum value in the schema. For instance, given
the following part of a schema:
    type Dog {
        name: String
    }
    type Human {
        pet: Dog
    }
1-1 renaming "Dog" to "NewDog" on a schema containing this object type (but not containing a
type named "NewDog") would produce a schema almost identical to the original schema except
with the NewDog object type replacing Dog everywhere it appears.
    type NewDog {
        name: String
    }
    type Human {
        pet: NewDog
    }
If "Dog" also appeared as a field in the schema's root type, it would be renamed to "NewDog" there
as well.

1-many renaming is an operation intended specifically for any of the following:
- fields in object types
- fields in interface types
- enum values
In 1-many renaming, the same field or enum value is mapped to multiple names. For instance, given
the following type in a schema:
    type Dog {
        name: String
    }
1-many renaming the "Dog" type's "name" field to "name" and "secondname" would produce a schema
almost identical to the original schema except with both fields representing the same underlying
data.
    type Dog {
        name: String
        secondname: String
    }

For both 1-1 or 1-many renaming, renamings only apply to types, fields, and enum values that exist
in the original schema. For example, if a schema contains a type named "Foo" and a type named "Bar"
but not a type named "Baz" and type_renamings maps "Foo" to "Bar" and "Bar" to "Baz", then the renamed
schema will contain a type named "Bar" (corresponding to the original schema's type "Foo") and a
type named "Baz" (corresponding to the original schema's type "Bar"), instead of containing two
types both named "Baz".

Suppressing part of the schema removes it altogether. For instance, given the following part of a
schema:
    type Dog {
        name: String
    }
suppressing "Dog" would produce an otherwise-identical schema but with that type (and therefore all
its fields) removed. If "Dog" also appeared as a field in the schema's root type, it would be
removed there as well.

Operations that are already supported:
- 1-1 renaming of object types, unions, enums, and interfaces.
- Suppressing types that don't implement an interface.
- Suppressing unions.

Operations that are not yet supported but will be implemented:
- Suppressions for fields, enums, enum values, interfaces, and types that implement interfaces.
- Renamings and suppressions for scalar types
- 1-1 and 1-many renamings for fields and enum values.

Renaming constraints:
- If you suppress all member types in a union, you must also suppress the union.
- If you suppress a type X, no other type Y may keep fields of type X (those fields must be
  suppressed, which requires field suppression which hasn't been implemented yet). However, if type
  X has a field of that type X, it is legal to suppress type X without explicitly suppressing that
  particular field.
- You may not suppress all types in the schema's root type.
- All names must be valid GraphQL names.
- Names may not conflict with each other. For instance, you may not rename both "Foo" and "Bar" to
  "Baz". You also may not rename anything to "Baz" if a type "Baz" already exists and is not also
  being renamed or suppressed.
- Special rules apply to the type_renamings argument if it's iterable (e.g. if a dict). If iterable,
  type_renamings may not contain no-op entries. In other words:
    - A string type_name may be in type_renamings only if there exists a type in the schema named
      type_name (since otherwise that entry would not affect any type in the schema).
    - A string type_name is in type_renamings, then type_renamings[type_name] != type_name (since applying the
      renaming would not change the type named type_name).
  If not iterable, then these no-op rules don't apply.
"""
from collections import namedtuple, defaultdict
from collections.abc import Iterable
from copy import copy
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple, Union, cast, DefaultDict

from graphql import (
    DocumentNode,
    EnumTypeDefinitionNode,
    FieldDefinitionNode,
    InterfaceTypeDefinitionNode,
    Node,
    ObjectTypeDefinitionNode,
    UnionTypeDefinitionNode,
    build_ast_schema,
)
from graphql.language.visitor import IDLE, REMOVE, Visitor, VisitorAction, visit
import six
from graphql.pyutils import FrozenList

from ..ast_manipulation import get_ast_with_non_null_and_list_stripped
from ..typedefs import Protocol
from .utils import (
    CascadingSuppressionError,
    InvalidTypeNameError,
    NoOpRenamingError,
    RenameTypes,
    RenameTypesT,
    SchemaRenameNameConflictError,
    SchemaTransformError,
    builtin_scalar_type_names,
    check_ast_schema_is_valid,
    get_copy_of_node_with_new_name,
    get_custom_scalar_names,
    get_query_type_name,
    is_valid_unreserved_name, RenamableFieldsT,
)


RenamedSchemaDescriptor = namedtuple(
    "RenamedSchemaDescriptor",
    (
        "schema_ast",  # Document, AST representing the renamed schema
        "schema",  # GraphQLSchema, representing the same schema as schema_ast
        "reverse_name_map",  # Dict[str, str], renamed type/query type field name to original name
        # reverse_name_map only contains names that were changed
        "reverse_field_name_map",  # Dict[str, Dict[str, str]], maps type name to a dict that maps each of the type's fields f (in the renamed schema) to the name of the field in the original schema.
        # reverse_field_name_map.get(type_name) is not None iff the type named type_name has different fields in the original schema vs. the renamed schema.
        # if type_name in reverse_field_name_map, reverse_field_name_map[type_name].get(new_field_name) is not None iff the new_field_name is the name of a field in the renamed schema that corresponds to a field in the original schema that is not also named new_field_name. e.g. if the field "id" is renamed to "id" and "new_id", only "new_id" -> "id" mapping will show up here because it's needed to rename the query as well.
    ),
)


# AST visitor functions can return a number of different things, such as returning a Node (to update
# that node) or returning a special value specified in graphql.visitor's VisitorAction.
VisitorReturnType = Union[Node, VisitorAction]


class TypeRenamingMapping(Protocol):
    def get(self, key: str, default: Optional[str]) -> Optional[str]:
        """Define mapping for type_renamings object."""
        ...


class FieldRenamingMapping(Protocol):
    class FieldRenamings(Protocol):
        def get(self, field_name: str, default: List[str]) -> List[str]:
            """Define mapping for renaming fields belonging to a type."""
            ...

    def get(self, type_name: str, default_field_renamings: FieldRenamings) -> Optional[FieldRenamings]:
        """Define mapping for a particular type's field renamings."""
        ...

def rename_schema(schema_ast: DocumentNode, type_renamings: TypeRenamingMapping, field_renamings: FieldRenamingMapping) -> RenamedSchemaDescriptor:
    """Create a RenamedSchemaDescriptor; rename/suppress types and root type fields using type_renamings.

    Any type, interface, enum, or fields of the root type/query type whose name
    appears in type_renamings will be renamed to the corresponding value if the value is not None. If the
    value is None, it will be suppressed in the renamed schema and queries will not be able to
    access it.

    Any such names that do not appear in type_renamings will be unchanged. Directives will never be
    renamed.

    In addition, some operations have not been implemented yet (see module-level docstring for more
    details).

    Args:
        schema_ast: represents a valid schema that does not contain extensions, input object
                    definitions, mutations, or subscriptions, whose fields of the query type share
                    the same name as the types they query. Not modified by this function
        type_renamings: maps original type name to renamed name or None (for type suppression). A type
                        named "Foo" will be unchanged iff renamings does not map "Foo" to anything, i.e.
                        renamings.get("Foo", "Foo") returns "Foo".
        field_renamings: maps type names to the renamings for its fields, which maps field names
                         belonging to the type to a list of field names for the renamed
                         schema.
    Returns:
        RenamedSchemaDescriptor containing the AST of the renamed schema, and the map of renamed
        type/field names to original names. Only renamed names will be included in the map.

    Raises:
        - CascadingSuppressionError if a type suppression would require further suppressions
        - SchemaTransformError if type_renamings suppressed every type. Note that this is a superclass of
          CascadingSuppressionError, InvalidTypeNameError, SchemaStructureError, and
          SchemaRenameNameConflictError, so handling exceptions of type SchemaTransformError will
          also catch all of its subclasses. This will change after the error classes are modified so
          that errors can be fixed programmatically, at which point it will make sense for the user
          to attempt to treat different errors differently
        - NotImplementedError if type_renamings attempts to suppress an enum, an interface, or a type
          implementing an interface
        - InvalidTypeNameError if the schema contains an invalid type name, or if the user attempts
          to rename a type to an invalid name. A name is considered invalid if it does not consist
          of alphanumeric characters and underscores, if it starts with a numeric character, or
          if it starts with double underscores
        - SchemaStructureError if the schema does not have the expected form; in particular, if
          the AST does not represent a valid schema, if any query type field does not have the
          same name as the type that it queries, if the schema contains type extensions or
          input object definitions, or if the schema contains mutations or subscriptions
        - SchemaRenameNameConflictError if there are conflicts between the renamed types or fields
    """
    # Check input schema satisfies various structural requirements
    check_ast_schema_is_valid(schema_ast)

    schema = build_ast_schema(schema_ast)
    query_type = get_query_type_name(schema)
    custom_scalar_names = get_custom_scalar_names(schema)

    _validate_renamings(schema_ast, type_renamings, field_renamings, query_type, custom_scalar_names)

    # Rename types, interfaces, enums, unions and suppress types, unions
    schema_ast, reverse_name_map, reverse_field_name_map = _rename_and_suppress_types_and_fields(
        schema_ast, type_renamings, field_renamings, query_type, custom_scalar_names
    )
    reverse_name_map_changed_names_only = {
        renamed_name: original_name
        for renamed_name, original_name in six.iteritems(reverse_name_map)
        if renamed_name != original_name
    }

    schema_ast = _rename_and_suppress_query_type_fields(schema_ast, type_renamings, query_type)
    return RenamedSchemaDescriptor(
        schema_ast=schema_ast,
        schema=build_ast_schema(schema_ast),
        reverse_name_map=reverse_name_map_changed_names_only,
        reverse_field_name_map=reverse_field_name_map
    )


def _validate_renamings(
    schema_ast: DocumentNode,
    type_renamings: TypeRenamingMapping,
    field_renamings: FieldRenamingMapping,
    query_type: str,
    custom_scalar_names: AbstractSet[str],
) -> None:
    """Validate the type_renamings argument before attempting to rename the schema.

    Check for fields with suppressed types or unions whose members were all suppressed. Also,
    confirm type_renamings contains no enums, interfaces, or interface implementation suppressions
    because that hasn't been implemented yet. Confirm that no scalars would be suppressed or
    renamed.

    The input AST will not be modified.

    Args:
        schema_ast: represents a valid schema that does not contain extensions, input object
                    definitions, mutations, or subscriptions, whose fields of the query type share
                    the same name as the types they query. Not modified by this function
        type_renamings: maps original type name to renamed name or None (for type suppression). A type
                        named "Foo" will be unchanged iff renamings does not map "Foo" to anything, i.e.
                        renamings.get("Foo", "Foo") returns "Foo".
        field_renamings: maps type names to the renamings for its fields, which maps field names
                         belonging to the type to a list of field names for the renamed
                         schema.
        query_type: name of the query type, e.g. 'RootSchemaQuery'
        custom_scalar_names: set of all user defined scalars used in the schema (excluding
                             builtin scalars)

    Raises:
        - CascadingSuppressionError if a type suppression would require further suppressions
        - NotImplementedError if type_renamings attempts to suppress an enum, an interface, or a type
          implementing an interface
    """
    _ensure_no_cascading_type_suppressions(schema_ast, type_renamings, query_type)
    _ensure_no_unsupported_operations(schema_ast, type_renamings, custom_scalar_names)


def _ensure_no_cascading_type_suppressions(
    schema_ast: DocumentNode, type_renamings: TypeRenamingMapping, query_type: str
) -> None:
    """Check for fields with suppressed types or unions whose members were all suppressed."""
    visitor = CascadingSuppressionCheckVisitor(type_renamings, query_type)
    visit(schema_ast, visitor)
    if visitor.fields_to_suppress or visitor.union_types_to_suppress:
        error_message_components = [
            f"Type renamings {type_renamings} would require further suppressions to produce a valid "
            f"renamed schema."
        ]
        if visitor.fields_to_suppress:
            for object_type in visitor.fields_to_suppress:
                error_message_components.append(f"Object type {object_type} contains: ")
                error_message_components.extend(
                    (
                        f"field {field} of suppressed type "
                        f"{visitor.fields_to_suppress[object_type][field]}, "
                        for field in visitor.fields_to_suppress[object_type]
                    )
                )
            error_message_components.append(
                "A schema containing a field that is of a nonexistent type is invalid. When field "
                "suppression is supported, you can fix this problem by suppressing the fields "
                "shown above."
            )
        if visitor.union_types_to_suppress:
            for union_type in visitor.union_types_to_suppress:
                error_message_components.append(
                    f"Union type {union_type} has no non-suppressed members: "
                )
                error_message_components.extend(
                    (union_member.name.value for union_member in union_type.types)
                )
            error_message_components.append(
                "To fix this, you can suppress the union as well by adding union_type: None to the "
                "type_renamings argument when renaming types, for each value of union_type described "
                "here. Note that adding suppressions may lead to other types, fields, etc. "
                "requiring suppression so you may need to iterate on this before getting a legal "
                "schema."
            )
        raise CascadingSuppressionError("\n".join(error_message_components))


def _ensure_no_unsupported_operations(
    schema_ast: DocumentNode,
    type_renamings: TypeRenamingMapping,
    custom_scalar_names: AbstractSet[str],
) -> None:
    """Check for unsupported type renaming or suppression operations."""
    _ensure_no_unsupported_scalar_operations(type_renamings, custom_scalar_names)
    _ensure_no_unsupported_suppressions(schema_ast, type_renamings)


def _ensure_no_unsupported_scalar_operations(
    type_renamings: TypeRenamingMapping,
    custom_scalar_names: AbstractSet[str],
) -> None:
    """Check for unsupported scalar operations."""
    unsupported_scalar_operations = {}  # Map scalars to value to be renamed.
    for scalar_name in custom_scalar_names:
        possibly_renamed_scalar_name = type_renamings.get(scalar_name, scalar_name)
        # type_renamings.get(scalar_name, scalar_name) returns something that is not scalar iff it
        # attempts to do something with the scalar (i.e. renaming or suppressing it)
        if possibly_renamed_scalar_name != scalar_name:
            unsupported_scalar_operations[scalar_name] = possibly_renamed_scalar_name
    for builtin_scalar_name in builtin_scalar_type_names:
        possibly_renamed_builtin_scalar_name = type_renamings.get(
            builtin_scalar_name, builtin_scalar_name
        )
        if possibly_renamed_builtin_scalar_name != builtin_scalar_name:
            # Check that built-in scalar types remain unchanged during type renaming.
            unsupported_scalar_operations[
                builtin_scalar_name
            ] = possibly_renamed_builtin_scalar_name
    if unsupported_scalar_operations:
        raise NotImplementedError(
            f"Scalar renaming and suppression is not implemented yet, but type_renamings attempted to "
            f"modify the following scalars: {unsupported_scalar_operations}. To fix this, remove "
            f"them from type_renamings."
        )


def _ensure_no_unsupported_suppressions(
    schema_ast: DocumentNode, type_renamings: TypeRenamingMapping
) -> None:
    """Confirm type_renamings contains no enums, interfaces, or interface implementation suppressions."""
    visitor = SuppressionNotImplementedVisitor(type_renamings)
    visit(schema_ast, visitor)
    if (
        not visitor.unsupported_enum_suppressions
        and not visitor.unsupported_interface_suppressions
        and not visitor.unsupported_interface_implementation_suppressions
    ):
        return
    # Otherwise, attempted to suppress something we shouldn't suppress.
    error_message_components = [
        f"Type renamings {type_renamings} attempted to suppress parts of the schema for which "
        f"suppression is not implemented yet."
    ]
    if visitor.unsupported_enum_suppressions:
        error_message_components.append(
            f"Type renamings mapped these schema enums to None: "
            f"{visitor.unsupported_enum_suppressions}, attempting to suppress them. However, "
            f"type renaming has not implemented enum suppression yet."
        )
    if visitor.unsupported_interface_suppressions:
        error_message_components.append(
            f"Type renamings mapped these schema interfaces to None: "
            f"{visitor.unsupported_interface_suppressions}, attempting to suppress them. However, "
            f"type renaming has not implemented interface suppression yet."
        )
    if visitor.unsupported_interface_implementation_suppressions:
        error_message_components.append(
            f"Type renamings mapped these object types to None: "
            f"{visitor.unsupported_interface_implementation_suppressions}, attempting to suppress "
            f"them. Normally, this would be fine. However, these types each implement at least one "
            f"interface and type renaming has not implemented this particular suppression yet."
        )
    error_message_components.append(
        "To avoid these suppressions, remove the mappings from the type_renamings argument."
    )
    raise NotImplementedError("\n".join(error_message_components))


def _rename_and_suppress_types_and_fields(
    schema_ast: DocumentNode,
    type_renamings: TypeRenamingMapping,
    field_renamings: FieldRenamingMapping,
    query_type: str,
    custom_scalar_names: AbstractSet[str],
) -> Tuple[DocumentNode, Dict[str, str], Dict[str, Dict[str, str]]]:
    """Rename and suppress types, enums, interfaces, fields using type_renamings, field_renamings.

    The query type will not be renamed.

    The input schema AST will not be modified.

    Args:
        schema_ast: schema that we're returning a modified version of
        type_renamings: maps original type name to renamed name or None (for type suppression). A type
                        named "Foo" will be unchanged iff renamings does not map "Foo" to anything, i.e.
                        renamings.get("Foo", "Foo") returns "Foo".
        field_renamings: maps type names to the renamings for its fields, which maps field names
                         belonging to the type to a list of field names for the renamed
                         schema.
        query_type: name of the query type, e.g. 'RootSchemaQuery'
        custom_scalar_names: set of all user defined scalars used in the schema (excluding
                             builtin scalars)

    Returns:
        Tuple containing the modified version of the schema AST, and the renamed type name to
        original type name map. Map contains all non-suppressed types, including those that were not
        renamed.

    Raises:
        - InvalidTypeNameError if the user attempts to rename a type to an invalid name
        - SchemaRenameNameConflictError if the rename causes name conflicts
    """
    visitor = RenameSchemaTypesVisitor(type_renamings, field_renamings, query_type, custom_scalar_names)
    renamed_schema_ast = visit(schema_ast, visitor)
    if visitor.invalid_type_names:
        raise InvalidTypeNameError(
            f"Applying the type renaming would rename types with names that are not valid, unreserved "
            f"GraphQL names. Valid, unreserved GraphQL names must consist of only alphanumeric "
            f"characters and underscores, must not start with a numeric character, and must not "
            f"start with double underscores. The following dictionary maps each type's original "
            f"name to what would be the new name: {visitor.invalid_type_names}"
        )
    if visitor.name_conflicts or visitor.renamed_to_builtin_scalar_conflicts:
        raise SchemaRenameNameConflictError(
            visitor.name_conflicts, visitor.renamed_to_builtin_scalar_conflicts
        )
    if isinstance(type_renamings, Iterable):
        # If type_renamings is iterable, then every renaming must be used and no renaming can map a
        # name to itself
        for type_name in visitor.suppressed_types:
            if type_name not in type_renamings:
                raise AssertionError(
                    f"suppressed_types should be a subset of the set of keys in type_renamings, but "
                    f"found {type_name} in suppressed_types that is not a key in type_renamings. This "
                    f"is a bug."
                )
        renamed_types = {
            visitor.reverse_name_map[type_name]
            for type_name in visitor.reverse_name_map
            if type_name != visitor.reverse_name_map[type_name]
        }
        no_op_type_renames: Set[str] = set(type_renamings) - renamed_types - set(visitor.suppressed_types)
        if no_op_type_renames or visitor.no_op_field_renamings:
            raise NoOpRenamingError(no_op_type_renames, visitor.no_op_field_renamings)
    return renamed_schema_ast, visitor.reverse_name_map, visitor.reverse_field_name_map


def _rename_and_suppress_query_type_fields(
    schema_ast: DocumentNode, type_renamings: TypeRenamingMapping, query_type: str
) -> DocumentNode:
    """Rename or suppress all fields of the query type.

    The input schema AST will not be modified.

    Args:
        schema_ast: schema that we're returning a modified version of
        type_renamings: maps original type name to renamed name or None (for type suppression). A type
                        named "Foo" will be unchanged iff renamings does not map "Foo" to anything, i.e.
                        renamings.get("Foo", "Foo") returns "Foo".
        query_type: name of the query type, e.g. 'RootSchemaQuery'

    Returns:
        modified version of the input schema AST

    Raises:
        - SchemaTransformError if type_renamings suppressed every type
    """
    visitor = RenameQueryTypeFieldsVisitor(type_renamings, query_type)
    renamed_schema_ast = visit(schema_ast, visitor)
    return renamed_schema_ast


class RenameSchemaTypesVisitor(Visitor):
    """Traverse a Document AST, editing the names of nodes."""

    noop_types = frozenset(
        {
            "ArgumentNode",
            "BooleanValueNode",
            "DirectiveNode",
            "DirectiveDefinitionNode",
            "DocumentNode",
            "EnumValueNode",
            "EnumValueDefinitionNode",
            "FieldNode",
            "FieldDefinitionNode",
            "FloatValueNode",
            "FragmentDefinitionNode",
            "FragmentSpreadNode",
            "InlineFragmentNode",
            "InputObjectTypeDefinitionNode",
            "InputValueDefinitionNode",
            "IntValueNode",
            "ListTypeNode",
            "ListValueNode",
            "NameNode",
            "NonNullTypeNode",
            "ObjectFieldNode",
            "ObjectValueNode",
            "OperationDefinitionNode",
            "OperationTypeDefinitionNode",
            "ScalarTypeDefinitionNode",
            "SchemaDefinitionNode",
            "SelectionSetNode",
            "StringValueNode",
            "VariableNode",
            "VariableDefinitionNode",
            "SchemaExtensionNode",
            "InterfaceTypeExtensionNode",
            "UnionTypeExtensionNode",
            "EnumTypeExtensionNode",
            "ObjectTypeExtensionNode",
            "InputObjectTypeExtensionNode",
            "ScalarTypeExtensionNode",
        }
    )
    # rename_types must be a set of strings corresponding to the names of the classes in
    # RenameTypes. The duplication exists because introspection for Unions via typing.get_args()
    # doesn't exist until Python 3.8. In Python 3.8, this would be a valid way to define
    # rename_types:
    # rename_types = frozenset(cls.__name__ for cls in get_args(RenameTypes))  # type: ignore
    # Note: even with Python 3.8, the mypy version at the time of writing (version 0.770) doesn't
    # allow for introspection for Unions. mypy's maintainers recently merged a PR
    # (https://github.com/python/mypy/pull/8779) that permits this line of code, but did so after
    # the mypy 0.770 release. If we do end up removing the duplication at a later point but not
    # update the mypy version, we'd need to ignore it (as shown in the in-line comment).
    rename_types = frozenset(
        {
            "EnumTypeDefinitionNode",
            "InterfaceTypeDefinitionNode",
            "NamedTypeNode",
            "ObjectTypeDefinitionNode",
            "UnionTypeDefinitionNode",
        }
    )
    # Collects naming conflict errors involving types that are not built-in scalar types. If
    # type_renamings would result in multiple types being named "Foo", name_conflicts will map "Foo" to a
    # set containing the name of each such type
    name_conflicts: Dict[str, Set[str]]
    # Collects naming conflict errors involving built-in scalar types. If type_renamings would rename a
    # type named "Foo" to "String", renamed_to_scalar_conflicts will map "Foo" to "String"
    renamed_to_builtin_scalar_conflicts: Dict[str, str]
    # reverse_name_map maps renamed type name to original type name, containing all non-suppressed
    # non-scalar types, including those that were unchanged. Must contain unchanged names to prevent
    # type renaming conflicts and raise SchemaRenameNameConflictError when they arise
    reverse_name_map: Dict[str, str]
    # Collects invalid type names in type_renamings. If type_renamings would rename a type named "Foo" to a
    # string that is not a valid, unreserved GraphQL type name (see definition in the
    # type_name_is_valid function in utils), invalid_type_names will map "Foo" to the invalid type
    # name.
    invalid_type_names: Dict[str, str]
    # Collects the type names for types that get suppressed. If type_renamings would suppress a type named
    # "Foo", renamed_types will contain "Foo".
    suppressed_types: Set[str]
    # reverse_field_name_map maps type name to a dict, which in turn maps the name of a field in the
    # renamed schema to the name of the field in the original schema, if the field has different
    # names in the original schema and the new schema. If field_renamings would rename a field named
    # "foo" to "bar" in a type named "Baz", then reverse_field_name_map["Baz"] will map "bar" to
    # "foo".
    reverse_field_name_map: DefaultDict[str, Dict[str, str]]
    # Maps no-op renamings for fields, mapping the type name (that contains the field) to the set of
    # names of fields for which field_renamings contained no-op renamings. Applies only when
    # field_renamings is iterable. If field_renamings would rename a field named "foo" to "foo" in a
    # type named "Bar", or if it would rename a field named "foo" in a type named "Bar" when such a
    # field does not exist, no_op_field_renamings will map "Bar" to a set containing "foo".
    # TODO wow this was pretty convoluted, see if you can re-word this
    no_op_field_renamings: DefaultDict[str, Set[str]]

    def __init__(
        self,
        type_renamings: TypeRenamingMapping,
        field_renamings: FieldRenamingMapping,
        query_type: str,
        custom_scalar_names: AbstractSet[str],
    ) -> None:
        """Create a visitor for renaming types in a schema AST.

        Args:
            type_renamings: maps original type name to renamed name or None (for type suppression). A
                            type named "Foo" will be unchanged iff renamings does not map "Foo" to
                            anything, i.e. renamings.get("Foo", "Foo") returns "Foo".
            field_renamings: maps type names to the renamings for its fields, which maps field names
                             belonging to the type to a list of field names for the renamed
                             schema.
            query_type: name of the query type (e.g. RootSchemaQuery), which will not be renamed
            custom_scalar_names: set of all user defined scalars used in the schema (excluding
                                 builtin scalars)
        """
        self.type_renamings = type_renamings
        self.reverse_name_map = {}
        self.name_conflicts = {}
        self.renamed_to_builtin_scalar_conflicts = {}
        self.invalid_type_names = {}
        self.query_type = query_type
        self.custom_scalar_names = frozenset(custom_scalar_names)
        self.suppressed_types = set()
        self.field_renamings = field_renamings
        self.reverse_field_name_map = defaultdict(dict)
        self.no_op_field_renamings = defaultdict(set)

    def _rename_or_suppress_or_ignore_name_and_add_to_record(
        self, node: RenameTypesT
    ) -> Union[RenameTypesT, VisitorAction]:
        """Specify input node change based on type_renamings. If node renamed, update reverse_name_map.

        Don't rename if the type is the query type or a builtin type.

        The input node will not be modified. reverse_name_map may be modified.

        Args:
            node: object representing an AST component, containing a .name attribute
                  corresponding to an AST node of type NameNode.

        Returns:
            Node object, REMOVE, or IDLE. The GraphQL library defines special return values REMOVE
            and IDLE to delete or do nothing with the node a visitor is currently at, respectively.
            If the current node is to be renamed, this function returns a Node object identical to
            the input node except with a new name. If it is to be suppressed, this function returns
            REMOVE. If neither of these are the case, this function returns IDLE.
        """
        type_name = node.name.value

        if (
            type_name == self.query_type
            or type_name in self.custom_scalar_names
            or type_name in builtin_scalar_type_names
        ):
            return IDLE

        desired_type_name = self.type_renamings.get(type_name, type_name)  # Default use original
        if desired_type_name is None:
            # Suppress the type
            self.suppressed_types.add(type_name)
            return REMOVE
        if not is_valid_unreserved_name(desired_type_name):
            self.invalid_type_names[type_name] = desired_type_name

        # Renaming conflict arises when two types with different names in the original schema have
        # the same name in the new schema. There are two ways to produce this conflict:
        # 1. when neither type is a custom scalar
        # 2. when one type is a custom scalar and the other is not
        #
        # If neither type is a custom scalar, then desired_type_name will be in
        # self.reverse_name_map (because self.reverse_name_map records all non-scalar types in
        # the schema). The types named self.reverse_name_map[desired_type_name] and type_name in
        # the old schema would both get mapped to desired_type_name in the new schema, even though
        # self.reverse_name_map[desired_type_name] != type_name.
        #
        # If one type in the conflict is a custom scalar, then the conflict arises from
        # attempting to rename a non-scalar type (from type_name to desired_type_name) when
        # there already exists a custom scalar type named desired_type_name. Custom scalar
        # renaming has not been implemented yet so we know for sure that the scalar's name (in
        # both the original and new schema) is desired_type_name.
        #
        # It's also possible to produce a similar conflict where one type is not a custom scalar but
        # rather a built-in scalar, e.g. String. That case is handled separately because renaming a
        # type to a built-in scalar will never be allowed in any schema, whereas the conflicts here
        # arise from conflicting with the existence of other types in the schema.
        if (
            self.reverse_name_map.get(desired_type_name, type_name) != type_name
            or desired_type_name in self.custom_scalar_names
        ):
            # If neither type in this conflict is a custom scalar, the two types causing conflict
            # were named type_name and self.reverse_name_map[desired_type_name] in the original
            # schema.
            # If one type in this conflict is a custom scalar, the two types causing conflict were
            # named type_name and desired_type_name (the latter being the custom scalar name) in the
            # original schema.
            conflictingly_renamed_type_name = self.reverse_name_map.get(
                desired_type_name, desired_type_name
            )

            # Collect all types in the original schema that would be named desired_type_name in the
            # new schema
            if desired_type_name not in self.name_conflicts:
                self.name_conflicts[desired_type_name] = {conflictingly_renamed_type_name}
            self.name_conflicts[desired_type_name].add(type_name)

        if desired_type_name in builtin_scalar_type_names:
            self.renamed_to_builtin_scalar_conflicts[type_name] = desired_type_name

        # At this point, the node will not be suppressed and its name in the new schema has been
        # validated, so it will appear in the renamed schema, so it's safe to rename its fields
        # if applicable. TODO re-word this
        fields_renamed_node = node  # by default, if no field renaming happens, fields_renamed_node will just be the node the visitor is at.
        if isinstance(node, (ObjectTypeDefinitionNode, InterfaceTypeDefinitionNode)):
            fields_renamed_node = self._rename_fields(node)
        self.reverse_name_map[desired_type_name] = type_name
        if desired_type_name == type_name:
            return fields_renamed_node
        else:  # Make copy of node with the changed name, return the copy
            node_with_new_name = get_copy_of_node_with_new_name(fields_renamed_node, desired_type_name)
            return node_with_new_name

    def _rename_fields(self, node: RenamableFieldsT) -> RenamableFieldsT:
        """Renames node's fields, if applicable, and return node with fields renamed."""
        if self.field_renamings.get(node.name.value, None) is None:
            # TODO: this is syntactically invalid if we don't give it the None argument, but we really don't want to have the argument required at all, but it seems we can't set default arguments in the class for some reason.
            return node
        field_nodes = set(node.fields)
        new_field_node_names = set()
        new_field_nodes = set()
        field_node_renamings = self.field_renamings.get(node.name.value, None)  # field renamings for this type in particular
        field_node_renamings_iterable = isinstance(field_node_renamings, Iterable)
        if field_node_renamings_iterable:
            field_renamings_to_be_used = set(field_node_renamings)  # toDO this is iffy bc field_renamings_to_be_used doesn't always show up and that makes Pycharm sad
        for field_node in field_nodes:
            original_field_name = field_node.name.value
            if field_node_renamings_iterable:
                # Check for no-op 1-1 renamings for fields
                if original_field_name in field_node_renamings and field_node_renamings[original_field_name] == [original_field_name]:
                    self.no_op_field_renamings[node.name.value].add(original_field_name)
            new_names = field_node_renamings.get(original_field_name, [original_field_name])  # TODO: same problem as before with the default None argument
            for new_field_name in new_names:
                if new_field_name in new_field_node_names:
                    # 'TODO fix this error message reporting for name conflicts'
                    raise SchemaRenameNameConflictError({}, {}, new_field_name)
                if not is_valid_unreserved_name(new_field_name):
                    raise InvalidTypeNameError("TODO make a better error collecting thing for this")
                # TODO can we implement this without parallel structures for new_field_node_names and new_field_nodes? Prone to errors, hard to reason about
                new_field_node_names.add(new_field_name)
                new_field_nodes.add(get_copy_of_node_with_new_name(field_node, new_field_name))
                if original_field_name != new_field_name:
                    self.reverse_field_name_map[node.name.value][new_field_name] = original_field_name
            if field_node_renamings_iterable:
                field_renamings_to_be_used.discard(original_field_name)  # discard to avoid raising errors if the renamings didn't explicitly specify this to be renamed, which is ok
        if field_node_renamings_iterable and field_renamings_to_be_used:
            # Need this condition because if all the renamings are used, calling update() will cause an empty set to be materialized anyways and then it'll seem like there are no-op field renamings when there aren't.
            self.no_op_field_renamings[node.name.value].update(field_renamings_to_be_used)  # add in all the nodes that weren't renamed
        new_type_node = copy(node)  # shallow copy is enough
        new_type_node.fields = FrozenList(new_field_nodes)
        return new_type_node



    def enter(
        self,
        node: Node,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> VisitorReturnType:
        """Upon entering a node, operate depending on node type."""
        node_type = type(node).__name__
        if node_type in self.noop_types:
            # Do nothing, continue traversal
            return IDLE
        elif node_type in self.rename_types:
            # Process the node by either renaming, suppressing, or not doing anything with it
            # (depending on what type_renamings specifies)
            return self._rename_or_suppress_or_ignore_name_and_add_to_record(
                cast(RenameTypes, node)
            )
        else:
            # All Node types should've been taken care of, this line should never be reached
            raise AssertionError('Unreachable code reached. Missed type: "{}"'.format(node_type))


class RenameQueryTypeFieldsVisitor(Visitor):
    def __init__(self, type_renamings: TypeRenamingMapping, query_type: str) -> None:
        """Create a visitor for renaming or suppressing fields of the query type in a schema AST.

        Args:
            type_renamings: maps original type name to renamed name or None (for type suppression). A
                            type named "Foo" will be unchanged iff renamings does not map "Foo" to
                            anything, i.e. renamings.get("Foo", "Foo") returns "Foo".
            query_type: name of the query type (e.g. RootSchemaQuery)

        Raises:
            - SchemaTransformError if every field in the query type was suppressed
        """
        # Note that as field names and type names have been confirmed to match up, any renamed
        # query type field already has a corresponding renamed type.
        self.in_query_type = False
        self.type_renamings = type_renamings
        self.query_type = query_type

    def enter_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If the node's name matches the query type, record that we entered the query type."""
        if node.name.value == self.query_type:
            self.in_query_type = True

    def leave_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If the node's name matches the query type, record that we left the query type."""
        if not node.fields:
            raise SchemaTransformError(
                f"Type renamings {self.type_renamings} suppressed every type in the schema so it will "
                f"be impossible to query for anything. To fix this, check why the `type_renamings` "
                f"argument of `rename_schema` mapped every type to None."
            )
        if node.name.value == self.query_type:
            self.in_query_type = False

    def enter_field_definition(
        self,
        node: FieldDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> VisitorReturnType:
        """If inside query type, rename or remove field as specified by type_renamings."""
        if self.in_query_type:
            field_name = node.name.value
            new_field_name = self.type_renamings.get(field_name, field_name)  # Default use original
            if new_field_name == field_name:
                return IDLE
            if new_field_name is None:
                # Suppress the type
                return REMOVE
            else:  # Make copy of node with the changed name, return the copy
                field_node_with_new_name = get_copy_of_node_with_new_name(node, new_field_name)
                return field_node_with_new_name

        return IDLE


class CascadingSuppressionCheckVisitor(Visitor):
    """Traverse the schema to check for cascading suppression issues.

    The fields_to_suppress attribute records non-suppressed fields that depend on suppressed types.
    The union_types_to_suppress attribute records unions that had all its members suppressed.

    After calling visit() on the schema using this visitor, if any of these attributes are non-empty
    then there are further suppressions required to produce a legal schema so the code should then
    raise a CascadingSuppressionError.

    """

    # For a type named T, and its field named F whose type has name V, this dict would be
    # {"T": {"F": "V"}}
    fields_to_suppress: Dict[str, Dict[str, str]]
    union_types_to_suppress: List[UnionTypeDefinitionNode]

    def __init__(self, type_renamings: TypeRenamingMapping, query_type: str) -> None:
        """Create a visitor to check that suppression does not cause an illegal state.

        Args:
            type_renamings: maps original type name to renamed name or None (for type suppression). A
                       type named "Foo" will be unchanged iff renamings does not map "Foo" to
                       anything, i.e. renamings.get("Foo", "Foo") returns "Foo".
            query_type: name of the query type (e.g. RootSchemaQuery)
        """
        self.type_renamings = type_renamings
        self.query_type = query_type
        self.current_type: Optional[str] = None
        self.fields_to_suppress = {}
        self.union_types_to_suppress = []

    def enter_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """Record the current type that the visitor is traversing."""
        self.current_type = node.name.value

    def leave_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """Finish traversing the current type node."""
        self.current_type = None

    def enter_field_definition(
        self,
        node: FieldDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """Check that no type Y contains a field of type X, where X is suppressed."""
        if self.current_type == self.query_type:
            return IDLE
        # At a field of a type that is not the query type
        field_name = node.name.value
        field_type = get_ast_with_non_null_and_list_stripped(node.type).name.value
        if self.type_renamings.get(field_type, field_type):
            return IDLE
        # Reaching this point means this field is of a type to be suppressed.
        if self.current_type is None:
            raise AssertionError(
                "Entered a field not in any ObjectTypeDefinition scope because "
                "self.current_type is None"
            )
        if self.current_type == field_type:
            # Then node corresponds to a field belonging to type T that is also of type T.
            # Therefore, we don't need to explicitly suppress the field as well and this should not
            # raise errors.
            return IDLE
        if self.current_type not in self.fields_to_suppress:
            self.fields_to_suppress[self.current_type] = {}
        self.fields_to_suppress[self.current_type][field_name] = field_type
        return IDLE

    def enter_union_type_definition(
        self,
        node: UnionTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """Check that each union still has at least one non-suppressed member."""
        union_name = node.name.value
        # Check if all the union members are suppressed.
        for union_member in node.types:
            union_member_type = get_ast_with_non_null_and_list_stripped(union_member).name.value
            if self.type_renamings.get(union_member_type, union_member_type):
                # Then at least one member of the union is not suppressed, so there is no cascading
                # suppression error concern.
                return IDLE
        if self.type_renamings.get(union_name, union_name) is None:
            # If the union is also suppressed, then nothing needs to happen here
            return IDLE
        self.union_types_to_suppress.append(node)

        return IDLE


class SuppressionNotImplementedVisitor(Visitor):
    """Traverse the schema to check for suppressions that are not yet implemented.

    Each attribute that mentions an unsupported suppression records the types that type_renamings
    attempts to suppress.

    After calling visit() on the schema using this visitor, if any of these attributes are non-empty
    then some suppressions specified by type_renamings are unsupported, so the code should then raise a
    NotImplementedError.

    """

    unsupported_enum_suppressions: Set[str]
    unsupported_interface_suppressions: Set[str]
    unsupported_interface_implementation_suppressions: Set[str]

    def __init__(self, type_renamings: TypeRenamingMapping) -> None:
        """Confirm type_renamings does not attempt to suppress enum/interface/interface implementation.

        Args:
            type_renamings: from original field name to renamed field name or None (for type
                       suppression). A type named "Foo" will be unchanged iff renamings does not map
                       "Foo" to anything, i.e. renamings.get("Foo", "Foo") returns "Foo".
        """
        self.type_renamings = type_renamings
        self.unsupported_enum_suppressions = set()
        self.unsupported_interface_suppressions = set()
        self.unsupported_interface_implementation_suppressions = set()

    def enter_enum_type_definition(
        self,
        node: EnumTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If type_renamings has enum suppression, record it for error message."""
        enum_name = node.name.value
        if self.type_renamings.get(enum_name, enum_name) is None:
            self.unsupported_enum_suppressions.add(enum_name)

    def enter_interface_type_definition(
        self,
        node: InterfaceTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If type_renamings has interface suppression, record it for error message."""
        interface_name = node.name.value
        if self.type_renamings.get(interface_name, interface_name) is None:
            self.unsupported_interface_suppressions.add(interface_name)

    def enter_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If type_renamings has interface implementation suppression, record it for error message."""
        if not node.interfaces:
            return
        object_name = node.name.value
        if self.type_renamings.get(object_name, object_name) is None:
            # Suppressing interface implementations isn't supported yet.
            self.unsupported_interface_implementation_suppressions.add(object_name)
