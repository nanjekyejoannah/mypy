from functools import wraps
import sys

from typing import (
    Tuple, Union, TypeVar, Callable, Sequence, Optional, Any, Dict, cast, List, overload
)
MYPY = False
if MYPY:
    import typing  # for typing.Type, which conflicts with types.Type
    from typing_extensions import Final

from mypy.sharedparse import (
    special_function_elide_names, argument_elide_name,
)
from mypy.nodes import (
    MypyFile, Node, ImportBase, Import, ImportAll, ImportFrom, FuncDef,
    OverloadedFuncDef, OverloadPart,
    ClassDef, Decorator, Block, Var, OperatorAssignmentStmt,
    ExpressionStmt, AssignmentStmt, ReturnStmt, RaiseStmt, AssertStmt,
    DelStmt, BreakStmt, ContinueStmt, PassStmt, GlobalDecl,
    WhileStmt, ForStmt, IfStmt, TryStmt, WithStmt,
    TupleExpr, GeneratorExpr, ListComprehension, ListExpr, ConditionalExpr,
    DictExpr, SetExpr, NameExpr, IntExpr, StrExpr, BytesExpr, UnicodeExpr,
    FloatExpr, CallExpr, SuperExpr, MemberExpr, IndexExpr, SliceExpr, OpExpr,
    UnaryExpr, LambdaExpr, ComparisonExpr,
    StarExpr, YieldFromExpr, NonlocalDecl, DictionaryComprehension,
    SetComprehension, ComplexExpr, EllipsisExpr, YieldExpr, Argument,
    AwaitExpr, TempNode, Expression, Statement,
    ARG_POS, ARG_OPT, ARG_STAR, ARG_NAMED, ARG_NAMED_OPT, ARG_STAR2,
    check_arg_names,
    FakeInfo,
)
from mypy.types import (
    Type, CallableType, AnyType, UnboundType, TupleType, TypeList, EllipsisType, CallableArgument,
    TypeOfAny, Instance,
)
from mypy import defaults
from mypy import messages
from mypy.errors import Errors
from mypy.options import Options

try:
    from typed_ast import ast3
    from typed_ast.ast3 import (
        AST,
        Call,
        FunctionType,
        Name,
        Attribute,
        Ellipsis as ast3_Ellipsis,
        Starred,
        NameConstant,
        Expression as ast3_Expression,
        Str,
        Index,
    )
except ImportError:
    if sys.version_info.minor > 2:
        try:
            from typed_ast import ast35  # type: ignore
        except ImportError:
            print('The typed_ast package is not installed.\n'
                  'You can install it with `python3 -m pip install typed-ast`.',
                  file=sys.stderr)
        else:
            print('You need a more recent version of the typed_ast package.\n'
                  'You can update to the latest version with '
                  '`python3 -m pip install -U typed-ast`.',
                  file=sys.stderr)
    else:
        print('Mypy requires the typed_ast package, which is only compatible with\n'
              'Python 3.3 and greater.', file=sys.stderr)
    sys.exit(1)

N = TypeVar('N', bound=Node)

# There is no way to create reasonable fallbacks at this stage,
# they must be patched later.
MISSING_FALLBACK = FakeInfo("fallback can't be filled out until semanal")  # type: Final
_dummy_fallback = Instance(MISSING_FALLBACK, [], -1)  # type: Final

TYPE_COMMENT_SYNTAX_ERROR = 'syntax error in type comment'  # type: Final
TYPE_COMMENT_AST_ERROR = 'invalid type comment or annotation'  # type: Final


# Older versions of typing don't allow using overload outside stubs,
# so provide a dummy.
# mypyc doesn't like function declarations nested in if statements
def _overload(x: Any) -> Any:
    return x


# mypyc doesn't like unreachable code, so trick mypy into thinking the branch is reachable
if bool() or sys.version_info < (3, 6):
    overload = _overload  # noqa


def parse(source: Union[str, bytes],
          fnam: str,
          module: Optional[str],
          errors: Optional[Errors] = None,
          options: Optional[Options] = None) -> MypyFile:

    """Parse a source file, without doing any semantic analysis.

    Return the parse tree. If errors is not provided, raise ParseError
    on failure. Otherwise, use the errors object to report parse errors.
    """
    raise_on_error = False
    if errors is None:
        errors = Errors()
        raise_on_error = True
    if options is None:
        options = Options()
    errors.set_file(fnam, module)
    is_stub_file = fnam.endswith('.pyi')
    try:
        if is_stub_file:
            feature_version = defaults.PYTHON3_VERSION[1]
        else:
            assert options.python_version[0] >= 3
            feature_version = options.python_version[1]
        ast = ast3.parse(source, fnam, 'exec', feature_version=feature_version)

        tree = ASTConverter(options=options,
                            is_stub=is_stub_file,
                            errors=errors,
                            ).visit(ast)
        tree.path = fnam
        tree.is_stub = is_stub_file
    except SyntaxError as e:
        errors.report(e.lineno, e.offset, e.msg, blocker=True)
        tree = MypyFile([], [], False, set())

    if raise_on_error and errors.is_errors():
        errors.raise_error()

    return tree


def parse_type_comment(type_comment: str, line: int, errors: Optional[Errors]) -> Optional[Type]:
    try:
        typ = ast3.parse(type_comment, '<type_comment>', 'eval')
    except SyntaxError as e:
        if errors is not None:
            errors.report(line, e.offset, TYPE_COMMENT_SYNTAX_ERROR, blocker=True)
            return None
        else:
            raise
    else:
        assert isinstance(typ, ast3_Expression)
        return TypeConverter(errors, line=line).visit(typ.body)


def is_no_type_check_decorator(expr: ast3.expr) -> bool:
    if isinstance(expr, Name):
        return expr.id == 'no_type_check'
    elif isinstance(expr, Attribute):
        if isinstance(expr.value, Name):
            return expr.value.id == 'typing' and expr.attr == 'no_type_check'
    return False


class ASTConverter:
    def __init__(self,
                 options: Options,
                 is_stub: bool,
                 errors: Errors) -> None:
        self.class_nesting = 0
        self.imports = []  # type: List[ImportBase]

        self.options = options
        self.is_stub = is_stub
        self.errors = errors

        # Cache of visit_X methods keyed by type of visited object
        self.visitor_cache = {}  # type: Dict[type, Callable[[Optional[AST]], Any]]

    def note(self, msg: str, line: int, column: int) -> None:
        self.errors.report(line, column, msg, severity='note')

    def fail(self, msg: str, line: int, column: int) -> None:
        self.errors.report(line, column, msg, blocker=True)

    def visit(self, node: Optional[AST]) -> Any:  # same as in typed_ast stub
        if node is None:
            return None
        typeobj = type(node)
        visitor = self.visitor_cache.get(typeobj)
        if visitor is None:
            method = 'visit_' + node.__class__.__name__
            visitor = getattr(self, method)
            self.visitor_cache[typeobj] = visitor
        return visitor(node)

    def set_line(self, node: N, n: Union[ast3.expr, ast3.stmt]) -> N:
        node.line = n.lineno
        node.column = n.col_offset
        return node

    def translate_expr_list(self, l: Sequence[AST]) -> List[Expression]:
        res = []  # type: List[Expression]
        for e in l:
            exp = self.visit(e)
            # assert isinstance(exp, Expression)
            res.append(exp)
        return res

    def translate_stmt_list(self, l: Sequence[AST]) -> List[Statement]:
        res = []  # type: List[Statement]
        for e in l:
            stmt = self.visit(e)
            # assert isinstance(stmt, Statement)
            res.append(stmt)
        return res

    op_map = {
        ast3.Add: '+',
        ast3.Sub: '-',
        ast3.Mult: '*',
        ast3.MatMult: '@',
        ast3.Div: '/',
        ast3.Mod: '%',
        ast3.Pow: '**',
        ast3.LShift: '<<',
        ast3.RShift: '>>',
        ast3.BitOr: '|',
        ast3.BitXor: '^',
        ast3.BitAnd: '&',
        ast3.FloorDiv: '//'
    }  # type: Final[Dict[typing.Type[AST], str]]

    def from_operator(self, op: ast3.operator) -> str:
        op_name = ASTConverter.op_map.get(type(op))
        if op_name is None:
            raise RuntimeError('Unknown operator ' + str(type(op)))
        else:
            return op_name

    comp_op_map = {
        ast3.Gt: '>',
        ast3.Lt: '<',
        ast3.Eq: '==',
        ast3.GtE: '>=',
        ast3.LtE: '<=',
        ast3.NotEq: '!=',
        ast3.Is: 'is',
        ast3.IsNot: 'is not',
        ast3.In: 'in',
        ast3.NotIn: 'not in'
    }  # type: Final[Dict[typing.Type[AST], str]]

    def from_comp_operator(self, op: ast3.cmpop) -> str:
        op_name = ASTConverter.comp_op_map.get(type(op))
        if op_name is None:
            raise RuntimeError('Unknown comparison operator ' + str(type(op)))
        else:
            return op_name

    def as_block(self, stmts: List[ast3.stmt], lineno: int) -> Optional[Block]:
        b = None
        if stmts:
            b = Block(self.fix_function_overloads(self.translate_stmt_list(stmts)))
            b.set_line(lineno)
        return b

    def as_required_block(self, stmts: List[ast3.stmt], lineno: int) -> Block:
        assert stmts  # must be non-empty
        b = Block(self.fix_function_overloads(self.translate_stmt_list(stmts)))
        b.set_line(lineno)
        return b

    def fix_function_overloads(self, stmts: List[Statement]) -> List[Statement]:
        ret = []  # type: List[Statement]
        current_overload = []  # type: List[OverloadPart]
        current_overload_name = None  # type: Optional[str]
        for stmt in stmts:
            if (current_overload_name is not None
                    and isinstance(stmt, (Decorator, FuncDef))
                    and stmt.name() == current_overload_name):
                current_overload.append(stmt)
            else:
                if len(current_overload) == 1:
                    ret.append(current_overload[0])
                elif len(current_overload) > 1:
                    ret.append(OverloadedFuncDef(current_overload))

                if isinstance(stmt, Decorator):
                    current_overload = [stmt]
                    current_overload_name = stmt.name()
                else:
                    current_overload = []
                    current_overload_name = None
                    ret.append(stmt)

        if len(current_overload) == 1:
            ret.append(current_overload[0])
        elif len(current_overload) > 1:
            ret.append(OverloadedFuncDef(current_overload))
        return ret

    def in_class(self) -> bool:
        return self.class_nesting > 0

    def translate_module_id(self, id: str) -> str:
        """Return the actual, internal module id for a source text id.

        For example, translate '__builtin__' in Python 2 to 'builtins'.
        """
        if id == self.options.custom_typing_module:
            return 'typing'
        elif id == '__builtin__' and self.options.python_version[0] == 2:
            # HACK: __builtin__ in Python 2 is aliases to builtins. However, the implementation
            #   is named __builtin__.py (there is another layer of translation elsewhere).
            return 'builtins'
        return id

    def visit_Module(self, mod: ast3.Module) -> MypyFile:
        body = self.fix_function_overloads(self.translate_stmt_list(mod.body))

        return MypyFile(body,
                        self.imports,
                        False,
                        {ti.lineno for ti in mod.type_ignores},
                        )

    # --- stmt ---
    # FunctionDef(identifier name, arguments args,
    #             stmt* body, expr* decorator_list, expr? returns, string? type_comment)
    # arguments = (arg* args, arg? vararg, arg* kwonlyargs, expr* kw_defaults,
    #              arg? kwarg, expr* defaults)
    def visit_FunctionDef(self, n: ast3.FunctionDef) -> Union[FuncDef, Decorator]:
        node = self.do_func_def(n)
        node.set_line(n.lineno, n.col_offset)
        return node

    # AsyncFunctionDef(identifier name, arguments args,
    #                  stmt* body, expr* decorator_list, expr? returns, string? type_comment)
    def visit_AsyncFunctionDef(self, n: ast3.AsyncFunctionDef) -> Union[FuncDef, Decorator]:
        node = self.do_func_def(n, is_coroutine=True)
        return self.set_line(node, n)

    def do_func_def(self, n: Union[ast3.FunctionDef, ast3.AsyncFunctionDef],
                    is_coroutine: bool = False) -> Union[FuncDef, Decorator]:
        """Helper shared between visit_FunctionDef and visit_AsyncFunctionDef."""
        no_type_check = bool(n.decorator_list and
                             any(is_no_type_check_decorator(d) for d in n.decorator_list))

        args = self.transform_args(n.args, n.lineno, no_type_check=no_type_check)

        arg_kinds = [arg.kind for arg in args]
        arg_names = [arg.variable.name() for arg in args]  # type: List[Optional[str]]
        arg_names = [None if argument_elide_name(name) else name for name in arg_names]
        if special_function_elide_names(n.name):
            arg_names = [None] * len(arg_names)
        arg_types = []  # type: List[Optional[Type]]
        if no_type_check:
            arg_types = [None] * len(args)
            return_type = None
        elif n.type_comment is not None:
            try:
                func_type_ast = ast3.parse(n.type_comment, '<func_type>', 'func_type')
                assert isinstance(func_type_ast, FunctionType)
                # for ellipsis arg
                if (len(func_type_ast.argtypes) == 1 and
                        isinstance(func_type_ast.argtypes[0], ast3_Ellipsis)):
                    if n.returns:
                        # PEP 484 disallows both type annotations and type comments
                        self.fail(messages.DUPLICATE_TYPE_SIGNATURES, n.lineno, n.col_offset)
                    arg_types = [a.type_annotation
                                 if a.type_annotation is not None
                                 else AnyType(TypeOfAny.unannotated)
                                 for a in args]
                else:
                    # PEP 484 disallows both type annotations and type comments
                    if n.returns or any(a.type_annotation is not None for a in args):
                        self.fail(messages.DUPLICATE_TYPE_SIGNATURES, n.lineno, n.col_offset)
                    translated_args = (TypeConverter(self.errors, line=n.lineno)
                                       .translate_expr_list(func_type_ast.argtypes))
                    arg_types = [a if a is not None else AnyType(TypeOfAny.unannotated)
                                for a in translated_args]
                return_type = TypeConverter(self.errors,
                                            line=n.lineno).visit(func_type_ast.returns)

                # add implicit self type
                if self.in_class() and len(arg_types) < len(args):
                    arg_types.insert(0, AnyType(TypeOfAny.special_form))
            except SyntaxError:
                self.fail(TYPE_COMMENT_SYNTAX_ERROR, n.lineno, n.col_offset)
                if n.type_comment and n.type_comment[0] != "(":
                    self.note('Suggestion: wrap argument types in parentheses',
                              n.lineno, n.col_offset)
                arg_types = [AnyType(TypeOfAny.from_error)] * len(args)
                return_type = AnyType(TypeOfAny.from_error)
        else:
            arg_types = [a.type_annotation for a in args]
            return_type = TypeConverter(self.errors, line=n.returns.lineno
                                        if n.returns else n.lineno).visit(n.returns)

        for arg, arg_type in zip(args, arg_types):
            self.set_type_optional(arg_type, arg.initializer)

        func_type = None
        if any(arg_types) or return_type:
            if len(arg_types) != 1 and any(isinstance(t, EllipsisType) for t in arg_types):
                self.fail("Ellipses cannot accompany other argument types "
                          "in function type signature.", n.lineno, 0)
            elif len(arg_types) > len(arg_kinds):
                self.fail('Type signature has too many arguments', n.lineno, 0)
            elif len(arg_types) < len(arg_kinds):
                self.fail('Type signature has too few arguments', n.lineno, 0)
            else:
                func_type = CallableType([a if a is not None else
                                          AnyType(TypeOfAny.unannotated) for a in arg_types],
                                         arg_kinds,
                                         arg_names,
                                         return_type if return_type is not None else
                                         AnyType(TypeOfAny.unannotated),
                                         _dummy_fallback)

        func_def = FuncDef(n.name,
                       args,
                       self.as_required_block(n.body, n.lineno),
                       func_type)
        if isinstance(func_def.type, CallableType):
            # semanal.py does some in-place modifications we want to avoid
            func_def.unanalyzed_type = func_def.type.copy_modified()
        if is_coroutine:
            func_def.is_coroutine = True
        if func_type is not None:
            func_type.definition = func_def
            func_type.line = n.lineno

        if n.decorator_list:
            var = Var(func_def.name())
            var.is_ready = False
            var.set_line(n.decorator_list[0].lineno)

            func_def.is_decorated = True
            func_def.set_line(n.lineno + len(n.decorator_list))
            func_def.body.set_line(func_def.get_line())
            return Decorator(func_def, self.translate_expr_list(n.decorator_list), var)
        else:
            return func_def

    def set_type_optional(self, type: Optional[Type], initializer: Optional[Expression]) -> None:
        if self.options.no_implicit_optional:
            return
        # Indicate that type should be wrapped in an Optional if arg is initialized to None.
        optional = isinstance(initializer, NameExpr) and initializer.name == 'None'
        if isinstance(type, UnboundType):
            type.optional = optional

    def transform_args(self,
                       args: ast3.arguments,
                       line: int,
                       no_type_check: bool = False,
                       ) -> List[Argument]:
        new_args = []
        names = []  # type: List[ast3.arg]
        num_no_defaults = len(args.args) - len(args.defaults)
        # positional arguments without defaults
        for a in args.args[:num_no_defaults]:
            new_args.append(self.make_argument(a, None, ARG_POS, no_type_check))
            names.append(a)

        # positional arguments with defaults
        for a, d in zip(args.args[num_no_defaults:], args.defaults):
            new_args.append(self.make_argument(a, d, ARG_OPT, no_type_check))
            names.append(a)

        # *arg
        if args.vararg is not None:
            new_args.append(self.make_argument(args.vararg, None, ARG_STAR, no_type_check))
            names.append(args.vararg)

        # keyword-only arguments with defaults
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            new_args.append(self.make_argument(
                a,
                d,
                ARG_NAMED if d is None else ARG_NAMED_OPT,
                no_type_check))
            names.append(a)

        # **kwarg
        if args.kwarg is not None:
            new_args.append(self.make_argument(args.kwarg, None, ARG_STAR2, no_type_check))
            names.append(args.kwarg)

        check_arg_names([name.arg for name in names], names, self.fail_arg)

        return new_args

    def make_argument(self, arg: ast3.arg, default: Optional[ast3.expr], kind: int,
                      no_type_check: bool) -> Argument:
        if no_type_check:
            arg_type = None
        else:
            if arg.annotation is not None and arg.type_comment is not None:
                self.fail(messages.DUPLICATE_TYPE_SIGNATURES, arg.lineno, arg.col_offset)
            arg_type = None
            if arg.annotation is not None:
                arg_type = TypeConverter(self.errors, line=arg.lineno).visit(arg.annotation)
            elif arg.type_comment is not None:
                arg_type = parse_type_comment(arg.type_comment, arg.lineno, self.errors)
        return Argument(Var(arg.arg), arg_type, self.visit(default), kind)

    def fail_arg(self, msg: str, arg: ast3.arg) -> None:
        self.fail(msg, arg.lineno, arg.col_offset)

    # ClassDef(identifier name,
    #  expr* bases,
    #  keyword* keywords,
    #  stmt* body,
    #  expr* decorator_list)
    def visit_ClassDef(self, n: ast3.ClassDef) -> ClassDef:
        self.class_nesting += 1
        keywords = [(kw.arg, self.visit(kw.value))
                    for kw in n.keywords if kw.arg]

        cdef = ClassDef(n.name,
                        self.as_required_block(n.body, n.lineno),
                        None,
                        self.translate_expr_list(n.bases),
                        metaclass=dict(keywords).get('metaclass'),
                        keywords=keywords)
        cdef.decorators = self.translate_expr_list(n.decorator_list)
        self.set_line(cdef, n)
        self.class_nesting -= 1
        return cdef

    # Return(expr? value)
    def visit_Return(self, n: ast3.Return) -> ReturnStmt:
        node = ReturnStmt(self.visit(n.value))
        return self.set_line(node, n)

    # Delete(expr* targets)
    def visit_Delete(self, n: ast3.Delete) -> DelStmt:
        if len(n.targets) > 1:
            tup = TupleExpr(self.translate_expr_list(n.targets))
            tup.set_line(n.lineno)
            node = DelStmt(tup)
        else:
            node = DelStmt(self.visit(n.targets[0]))
        return self.set_line(node, n)

    # Assign(expr* targets, expr? value, string? type_comment, expr? annotation)
    def visit_Assign(self, n: ast3.Assign) -> AssignmentStmt:
        lvalues = self.translate_expr_list(n.targets)
        rvalue = self.visit(n.value)
        if n.type_comment is not None:
            typ = parse_type_comment(n.type_comment, n.lineno, self.errors)
        else:
            typ = None
        s = AssignmentStmt(lvalues, rvalue, type=typ, new_syntax=False)
        return self.set_line(s, n)

    # AnnAssign(expr target, expr annotation, expr? value, int simple)
    def visit_AnnAssign(self, n: ast3.AnnAssign) -> AssignmentStmt:
        if n.value is None:  # always allow 'x: int'
            rvalue = TempNode(AnyType(TypeOfAny.special_form), no_rhs=True)  # type: Expression
        else:
            rvalue = self.visit(n.value)
        typ = TypeConverter(self.errors, line=n.lineno).visit(n.annotation)
        assert typ is not None
        typ.column = n.annotation.col_offset
        s = AssignmentStmt([self.visit(n.target)], rvalue, type=typ, new_syntax=True)
        return self.set_line(s, n)

    # AugAssign(expr target, operator op, expr value)
    def visit_AugAssign(self, n: ast3.AugAssign) -> OperatorAssignmentStmt:
        s = OperatorAssignmentStmt(self.from_operator(n.op),
                                   self.visit(n.target),
                                   self.visit(n.value))
        return self.set_line(s, n)

    # For(expr target, expr iter, stmt* body, stmt* orelse, string? type_comment)
    def visit_For(self, n: ast3.For) -> ForStmt:
        if n.type_comment is not None:
            target_type = parse_type_comment(n.type_comment, n.lineno, self.errors)
        else:
            target_type = None
        node = ForStmt(self.visit(n.target),
                       self.visit(n.iter),
                       self.as_required_block(n.body, n.lineno),
                       self.as_block(n.orelse, n.lineno),
                       target_type)
        return self.set_line(node, n)

    # AsyncFor(expr target, expr iter, stmt* body, stmt* orelse, string? type_comment)
    def visit_AsyncFor(self, n: ast3.AsyncFor) -> ForStmt:
        if n.type_comment is not None:
            target_type = parse_type_comment(n.type_comment, n.lineno, self.errors)
        else:
            target_type = None
        node = ForStmt(self.visit(n.target),
                       self.visit(n.iter),
                       self.as_required_block(n.body, n.lineno),
                       self.as_block(n.orelse, n.lineno),
                       target_type)
        node.is_async = True
        return self.set_line(node, n)

    # While(expr test, stmt* body, stmt* orelse)
    def visit_While(self, n: ast3.While) -> WhileStmt:
        node = WhileStmt(self.visit(n.test),
                         self.as_required_block(n.body, n.lineno),
                         self.as_block(n.orelse, n.lineno))
        return self.set_line(node, n)

    # If(expr test, stmt* body, stmt* orelse)
    def visit_If(self, n: ast3.If) -> IfStmt:
        lineno = n.lineno
        node = IfStmt([self.visit(n.test)],
                      [self.as_required_block(n.body, lineno)],
                      self.as_block(n.orelse, lineno))
        return self.set_line(node, n)

    # With(withitem* items, stmt* body, string? type_comment)
    def visit_With(self, n: ast3.With) -> WithStmt:
        if n.type_comment is not None:
            target_type = parse_type_comment(n.type_comment, n.lineno, self.errors)
        else:
            target_type = None
        node = WithStmt([self.visit(i.context_expr) for i in n.items],
                        [self.visit(i.optional_vars) for i in n.items],
                        self.as_required_block(n.body, n.lineno),
                        target_type)
        return self.set_line(node, n)

    # AsyncWith(withitem* items, stmt* body, string? type_comment)
    def visit_AsyncWith(self, n: ast3.AsyncWith) -> WithStmt:
        if n.type_comment is not None:
            target_type = parse_type_comment(n.type_comment, n.lineno, self.errors)
        else:
            target_type = None
        s = WithStmt([self.visit(i.context_expr) for i in n.items],
                     [self.visit(i.optional_vars) for i in n.items],
                     self.as_required_block(n.body, n.lineno),
                     target_type)
        s.is_async = True
        return self.set_line(s, n)

    # Raise(expr? exc, expr? cause)
    def visit_Raise(self, n: ast3.Raise) -> RaiseStmt:
        node = RaiseStmt(self.visit(n.exc), self.visit(n.cause))
        return self.set_line(node, n)

    # Try(stmt* body, excepthandler* handlers, stmt* orelse, stmt* finalbody)
    def visit_Try(self, n: ast3.Try) -> TryStmt:
        vs = [NameExpr(h.name) if h.name is not None else None for h in n.handlers]
        types = [self.visit(h.type) for h in n.handlers]
        handlers = [self.as_required_block(h.body, h.lineno) for h in n.handlers]

        node = TryStmt(self.as_required_block(n.body, n.lineno),
                       vs,
                       types,
                       handlers,
                       self.as_block(n.orelse, n.lineno),
                       self.as_block(n.finalbody, n.lineno))
        return self.set_line(node, n)

    # Assert(expr test, expr? msg)
    def visit_Assert(self, n: ast3.Assert) -> AssertStmt:
        node = AssertStmt(self.visit(n.test), self.visit(n.msg))
        return self.set_line(node, n)

    # Import(alias* names)
    def visit_Import(self, n: ast3.Import) -> Import:
        names = []  # type: List[Tuple[str, Optional[str]]]
        for alias in n.names:
            name = self.translate_module_id(alias.name)
            asname = alias.asname
            if asname is None and name != alias.name:
                # if the module name has been translated (and it's not already
                # an explicit import-as), make it an implicit import-as the
                # original name
                asname = alias.name
            names.append((name, asname))
        i = Import(names)
        self.imports.append(i)
        return self.set_line(i, n)

    # ImportFrom(identifier? module, alias* names, int? level)
    def visit_ImportFrom(self, n: ast3.ImportFrom) -> ImportBase:
        assert n.level is not None
        if len(n.names) == 1 and n.names[0].name == '*':
            mod = n.module if n.module is not None else ''
            i = ImportAll(mod, n.level)  # type: ImportBase
        else:
            i = ImportFrom(self.translate_module_id(n.module) if n.module is not None else '',
                           n.level,
                           [(a.name, a.asname) for a in n.names])
        self.imports.append(i)
        return self.set_line(i, n)

    # Global(identifier* names)
    def visit_Global(self, n: ast3.Global) -> GlobalDecl:
        g = GlobalDecl(n.names)
        return self.set_line(g, n)

    # Nonlocal(identifier* names)
    def visit_Nonlocal(self, n: ast3.Nonlocal) -> NonlocalDecl:
        d = NonlocalDecl(n.names)
        return self.set_line(d, n)

    # Expr(expr value)
    def visit_Expr(self, n: ast3.Expr) -> ExpressionStmt:
        value = self.visit(n.value)
        node = ExpressionStmt(value)
        return self.set_line(node, n)

    # Pass
    def visit_Pass(self, n: ast3.Pass) -> PassStmt:
        s = PassStmt()
        return self.set_line(s, n)

    # Break
    def visit_Break(self, n: ast3.Break) -> BreakStmt:
        s = BreakStmt()
        return self.set_line(s, n)

    # Continue
    def visit_Continue(self, n: ast3.Continue) -> ContinueStmt:
        s = ContinueStmt()
        return self.set_line(s, n)

    # --- expr ---

    # BoolOp(boolop op, expr* values)
    def visit_BoolOp(self, n: ast3.BoolOp) -> OpExpr:
        # mypy translates (1 and 2 and 3) as (1 and (2 and 3))
        assert len(n.values) >= 2
        op_node = n.op
        if isinstance(op_node, ast3.And):
            op = 'and'
        elif isinstance(op_node, ast3.Or):
            op = 'or'
        else:
            raise RuntimeError('unknown BoolOp ' + str(type(n)))

        # potentially inefficient!
        return self.group(op, self.translate_expr_list(n.values), n)

    def group(self, op: str, vals: List[Expression], n: ast3.expr) -> OpExpr:
        if len(vals) == 2:
            e = OpExpr(op, vals[0], vals[1])
        else:
            e = OpExpr(op, vals[0], self.group(op, vals[1:], n))
        return self.set_line(e, n)

    # BinOp(expr left, operator op, expr right)
    def visit_BinOp(self, n: ast3.BinOp) -> OpExpr:
        op = self.from_operator(n.op)

        if op is None:
            raise RuntimeError('cannot translate BinOp ' + str(type(n.op)))

        e = OpExpr(op, self.visit(n.left), self.visit(n.right))
        return self.set_line(e, n)

    # UnaryOp(unaryop op, expr operand)
    def visit_UnaryOp(self, n: ast3.UnaryOp) -> UnaryExpr:
        op = None
        if isinstance(n.op, ast3.Invert):
            op = '~'
        elif isinstance(n.op, ast3.Not):
            op = 'not'
        elif isinstance(n.op, ast3.UAdd):
            op = '+'
        elif isinstance(n.op, ast3.USub):
            op = '-'

        if op is None:
            raise RuntimeError('cannot translate UnaryOp ' + str(type(n.op)))

        e = UnaryExpr(op, self.visit(n.operand))
        return self.set_line(e, n)

    # Lambda(arguments args, expr body)
    def visit_Lambda(self, n: ast3.Lambda) -> LambdaExpr:
        body = ast3.Return(n.body)
        body.lineno = n.lineno
        body.col_offset = n.col_offset

        e = LambdaExpr(self.transform_args(n.args, n.lineno),
                       self.as_required_block([body], n.lineno))
        return self.set_line(e, n)

    # IfExp(expr test, expr body, expr orelse)
    def visit_IfExp(self, n: ast3.IfExp) -> ConditionalExpr:
        e = ConditionalExpr(self.visit(n.test),
                            self.visit(n.body),
                            self.visit(n.orelse))
        return self.set_line(e, n)

    # Dict(expr* keys, expr* values)
    def visit_Dict(self, n: ast3.Dict) -> DictExpr:
        e = DictExpr(list(zip(self.translate_expr_list(n.keys),
                              self.translate_expr_list(n.values))))
        return self.set_line(e, n)

    # Set(expr* elts)
    def visit_Set(self, n: ast3.Set) -> SetExpr:
        e = SetExpr(self.translate_expr_list(n.elts))
        return self.set_line(e, n)

    # ListComp(expr elt, comprehension* generators)
    def visit_ListComp(self, n: ast3.ListComp) -> ListComprehension:
        e = ListComprehension(self.visit_GeneratorExp(cast(ast3.GeneratorExp, n)))
        return self.set_line(e, n)

    # SetComp(expr elt, comprehension* generators)
    def visit_SetComp(self, n: ast3.SetComp) -> SetComprehension:
        e = SetComprehension(self.visit_GeneratorExp(cast(ast3.GeneratorExp, n)))
        return self.set_line(e, n)

    # DictComp(expr key, expr value, comprehension* generators)
    def visit_DictComp(self, n: ast3.DictComp) -> DictionaryComprehension:
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.translate_expr_list(c.ifs) for c in n.generators]
        is_async = [bool(c.is_async) for c in n.generators]
        e = DictionaryComprehension(self.visit(n.key),
                                    self.visit(n.value),
                                    targets,
                                    iters,
                                    ifs_list,
                                    is_async)
        return self.set_line(e, n)

    # GeneratorExp(expr elt, comprehension* generators)
    def visit_GeneratorExp(self, n: ast3.GeneratorExp) -> GeneratorExpr:
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.translate_expr_list(c.ifs) for c in n.generators]
        is_async = [bool(c.is_async) for c in n.generators]
        e = GeneratorExpr(self.visit(n.elt),
                          targets,
                          iters,
                          ifs_list,
                          is_async)
        return self.set_line(e, n)

    # Await(expr value)
    def visit_Await(self, n: ast3.Await) -> AwaitExpr:
        v = self.visit(n.value)
        e = AwaitExpr(v)
        return self.set_line(e, n)

    # Yield(expr? value)
    def visit_Yield(self, n: ast3.Yield) -> YieldExpr:
        e = YieldExpr(self.visit(n.value))
        return self.set_line(e, n)

    # YieldFrom(expr value)
    def visit_YieldFrom(self, n: ast3.YieldFrom) -> YieldFromExpr:
        e = YieldFromExpr(self.visit(n.value))
        return self.set_line(e, n)

    # Compare(expr left, cmpop* ops, expr* comparators)
    def visit_Compare(self, n: ast3.Compare) -> ComparisonExpr:
        operators = [self.from_comp_operator(o) for o in n.ops]
        operands = self.translate_expr_list([n.left] + n.comparators)
        e = ComparisonExpr(operators, operands)
        return self.set_line(e, n)

    # Call(expr func, expr* args, keyword* keywords)
    # keyword = (identifier? arg, expr value)
    def visit_Call(self, n: Call) -> CallExpr:
        args = n.args
        keywords = n.keywords
        arg_types = self.translate_expr_list(
            [a.value if isinstance(a, Starred) else a for a in args] +
            [k.value for k in keywords])
        arg_kinds = ([ARG_STAR if isinstance(a, Starred) else ARG_POS for a in args] +
                     [ARG_STAR2 if k.arg is None else ARG_NAMED for k in keywords])
        e = CallExpr(self.visit(n.func),
                     arg_types,
                     arg_kinds,
                     cast('List[Optional[str]]', [None] * len(args)) +
                     [k.arg for k in keywords])
        return self.set_line(e, n)

    # Num(object n) -- a number as a PyObject.
    def visit_Num(self, n: ast3.Num) -> Union[IntExpr, FloatExpr, ComplexExpr]:
        val = n.n
        if isinstance(val, int):
            e = IntExpr(val)  # type: Union[IntExpr, FloatExpr, ComplexExpr]
        elif isinstance(val, float):
            e = FloatExpr(val)
        elif isinstance(val, complex):
            e = ComplexExpr(val)
        else:
            raise RuntimeError('num not implemented for ' + str(type(val)))
        return self.set_line(e, n)

    # Str(string s)
    def visit_Str(self, n: Str) -> Union[UnicodeExpr, StrExpr]:
        # Hack: assume all string literals in Python 2 stubs are normal
        # strs (i.e. not unicode).  All stubs are parsed with the Python 3
        # parser, which causes unprefixed string literals to be interpreted
        # as unicode instead of bytes.  This hack is generally okay,
        # because mypy considers str literals to be compatible with
        # unicode.
        e = StrExpr(n.s)
        return self.set_line(e, n)

    # JoinedStr(expr* values)
    def visit_JoinedStr(self, n: ast3.JoinedStr) -> Expression:
        # Each of n.values is a str or FormattedValue; we just concatenate
        # them all using ''.join.
        empty_string = StrExpr('')
        empty_string.set_line(n.lineno, n.col_offset)
        strs_to_join = ListExpr(self.translate_expr_list(n.values))
        strs_to_join.set_line(empty_string)
        join_method = MemberExpr(empty_string, 'join')
        join_method.set_line(empty_string)
        result_expression = CallExpr(join_method,
                                     [strs_to_join],
                                     [ARG_POS],
                                     [None])
        return self.set_line(result_expression, n)

    # FormattedValue(expr value)
    def visit_FormattedValue(self, n: ast3.FormattedValue) -> Expression:
        # A FormattedValue is a component of a JoinedStr, or it can exist
        # on its own. We translate them to individual '{}'.format(value)
        # calls -- we don't bother with the conversion/format_spec fields.
        exp = self.visit(n.value)
        exp.set_line(n.lineno, n.col_offset)
        format_string = StrExpr('{}')
        format_string.set_line(n.lineno, n.col_offset)
        format_method = MemberExpr(format_string, 'format')
        format_method.set_line(format_string)
        result_expression = CallExpr(format_method,
                                     [exp],
                                     [ARG_POS],
                                     [None])
        return self.set_line(result_expression, n)

    # Bytes(bytes s)
    def visit_Bytes(self, n: ast3.Bytes) -> Union[BytesExpr, StrExpr]:
        # The following line is a bit hacky, but is the best way to maintain
        # compatibility with how mypy currently parses the contents of bytes literals.
        contents = str(n.s)[2:-1]
        e = BytesExpr(contents)
        return self.set_line(e, n)

    # NameConstant(singleton value)
    def visit_NameConstant(self, n: NameConstant) -> NameExpr:
        return NameExpr(str(n.value))

    # Ellipsis
    def visit_Ellipsis(self, n: ast3_Ellipsis) -> EllipsisExpr:
        e = EllipsisExpr()
        return self.set_line(e, n)

    # Attribute(expr value, identifier attr, expr_context ctx)
    def visit_Attribute(self, n: Attribute) -> Union[MemberExpr, SuperExpr]:
        value = n.value
        if (isinstance(value, Call) and
                isinstance(value.func, Name) and
                value.func.id == 'super'):
            e = SuperExpr(n.attr, self.visit(value))  # type: Union[MemberExpr, SuperExpr]
        else:
            e = MemberExpr(self.visit(value), n.attr)
        return self.set_line(e, n)

    # Subscript(expr value, slice slice, expr_context ctx)
    def visit_Subscript(self, n: ast3.Subscript) -> IndexExpr:
        e = IndexExpr(self.visit(n.value), self.visit(n.slice))
        return self.set_line(e, n)

    # Starred(expr value, expr_context ctx)
    def visit_Starred(self, n: Starred) -> StarExpr:
        e = StarExpr(self.visit(n.value))
        return self.set_line(e, n)

    # Name(identifier id, expr_context ctx)
    def visit_Name(self, n: Name) -> NameExpr:
        e = NameExpr(n.id)
        return self.set_line(e, n)

    # List(expr* elts, expr_context ctx)
    def visit_List(self, n: ast3.List) -> Union[ListExpr, TupleExpr]:
        expr_list = [self.visit(e) for e in n.elts]  # type: List[Expression]
        if isinstance(n.ctx, ast3.Store):
            # [x, y] = z and (x, y) = z means exactly the same thing
            e = TupleExpr(expr_list)  # type: Union[ListExpr, TupleExpr]
        else:
            e = ListExpr(expr_list)
        return self.set_line(e, n)

    # Tuple(expr* elts, expr_context ctx)
    def visit_Tuple(self, n: ast3.Tuple) -> TupleExpr:
        e = TupleExpr([self.visit(e) for e in n.elts])
        return self.set_line(e, n)

    # --- slice ---

    # Slice(expr? lower, expr? upper, expr? step)
    def visit_Slice(self, n: ast3.Slice) -> SliceExpr:
        return SliceExpr(self.visit(n.lower),
                         self.visit(n.upper),
                         self.visit(n.step))

    # ExtSlice(slice* dims)
    def visit_ExtSlice(self, n: ast3.ExtSlice) -> TupleExpr:
        return TupleExpr(self.translate_expr_list(n.dims))

    # Index(expr value)
    def visit_Index(self, n: Index) -> Node:
        return self.visit(n.value)


class TypeConverter:
    def __init__(self, errors: Optional[Errors], line: int = -1) -> None:
        self.errors = errors
        self.line = line
        self.node_stack = []  # type: List[AST]

    @overload
    def visit(self, node: ast3.expr) -> Type: ...

    @overload  # noqa
    def visit(self, node: Optional[AST]) -> Optional[Type]: ...

    def visit(self, node: Optional[AST]) -> Optional[Type]:  # noqa
        """Modified visit -- keep track of the stack of nodes"""
        if node is None:
            return None
        self.node_stack.append(node)
        try:
            method = 'visit_' + node.__class__.__name__
            visitor = getattr(self, method, None)
            if visitor is not None:
                return visitor(node)
            else:
                self.fail(TYPE_COMMENT_AST_ERROR, self.line, getattr(node, 'col_offset', -1))
                return AnyType(TypeOfAny.from_error)
        finally:
            self.node_stack.pop()

    def parent(self) -> Optional[AST]:
        """Return the AST node above the one we are processing"""
        if len(self.node_stack) < 2:
            return None
        return self.node_stack[-2]

    def fail(self, msg: str, line: int, column: int) -> None:
        if self.errors:
            self.errors.report(line, column, msg, blocker=True)

    def note(self, msg: str, line: int, column: int) -> None:
        if self.errors:
            self.errors.report(line, column, msg, severity='note')

    def visit_raw_str(self, s: str) -> Type:
        # An escape hatch that allows the AST walker in fastparse2 to
        # directly hook into the Python 3.5 type converter in some cases
        # without needing to create an intermediary `Str` object.
        return (parse_type_comment(s.strip(), self.line, self.errors) or
                AnyType(TypeOfAny.from_error))

    def translate_expr_list(self, l: Sequence[ast3.expr]) -> List[Type]:
        return [self.visit(e) for e in l]

    def visit_Call(self, e: Call) -> Type:
        # Parse the arg constructor
        f = e.func
        constructor = stringify_name(f)

        if not isinstance(self.parent(), ast3.List):
            self.fail(TYPE_COMMENT_AST_ERROR, self.line, e.col_offset)
            if constructor:
                self.note("Suggestion: use {}[...] instead of {}(...)".format(
                    constructor, constructor),
                    self.line, e.col_offset)
            return AnyType(TypeOfAny.from_error)
        if not constructor:
            self.fail("Expected arg constructor name", e.lineno, e.col_offset)

        name = None  # type: Optional[str]
        default_type = AnyType(TypeOfAny.special_form)
        typ = default_type  # type: Type
        for i, arg in enumerate(e.args):
            if i == 0:
                converted = self.visit(arg)
                assert converted is not None
                typ = converted
            elif i == 1:
                name = self._extract_argument_name(arg)
            else:
                self.fail("Too many arguments for argument constructor",
                          f.lineno, f.col_offset)
        for k in e.keywords:
            value = k.value
            if k.arg == "name":
                if name is not None:
                    self.fail('"{}" gets multiple values for keyword argument "name"'.format(
                        constructor), f.lineno, f.col_offset)
                name = self._extract_argument_name(value)
            elif k.arg == "type":
                if typ is not default_type:
                    self.fail('"{}" gets multiple values for keyword argument "type"'.format(
                        constructor), f.lineno, f.col_offset)
                converted = self.visit(value)
                assert converted is not None
                typ = converted
            else:
                self.fail(
                    'Unexpected argument "{}" for argument constructor'.format(k.arg),
                    value.lineno, value.col_offset)
        return CallableArgument(typ, name, constructor, e.lineno, e.col_offset)

    def translate_argument_list(self, l: Sequence[ast3.expr]) -> TypeList:
        return TypeList([self.visit(e) for e in l], line=self.line)

    def _extract_argument_name(self, n: ast3.expr) -> Optional[str]:
        if isinstance(n, Str):
            return n.s.strip()
        elif isinstance(n, NameConstant) and str(n.value) == 'None':
            return None
        self.fail('Expected string literal for argument name, got {}'.format(
            type(n).__name__), self.line, 0)
        return None

    def visit_Name(self, n: Name) -> Type:
        return UnboundType(n.id, line=self.line)

    def visit_NameConstant(self, n: NameConstant) -> Type:
        return UnboundType(str(n.value))

    # Str(string s)
    def visit_Str(self, n: Str) -> Type:
        return (parse_type_comment(n.s.strip(), self.line, self.errors) or
                AnyType(TypeOfAny.from_error))

    # Subscript(expr value, slice slice, expr_context ctx)
    def visit_Subscript(self, n: ast3.Subscript) -> Type:
        if not isinstance(n.slice, Index):
            self.fail(TYPE_COMMENT_SYNTAX_ERROR, self.line, getattr(n, 'col_offset', -1))
            return AnyType(TypeOfAny.from_error)

        empty_tuple_index = False
        if isinstance(n.slice.value, ast3.Tuple):
            params = self.translate_expr_list(n.slice.value.elts)
            if len(n.slice.value.elts) == 0:
                empty_tuple_index = True
        else:
            params = [self.visit(n.slice.value)]

        value = self.visit(n.value)
        if isinstance(value, UnboundType) and not value.args:
            return UnboundType(value.name, params, line=self.line,
                               empty_tuple_index=empty_tuple_index)
        else:
            self.fail(TYPE_COMMENT_AST_ERROR, self.line, getattr(n, 'col_offset', -1))
            return AnyType(TypeOfAny.from_error)

    def visit_Tuple(self, n: ast3.Tuple) -> Type:
        return TupleType(self.translate_expr_list(n.elts), _dummy_fallback,
                         implicit=True, line=self.line)

    # Attribute(expr value, identifier attr, expr_context ctx)
    def visit_Attribute(self, n: Attribute) -> Type:
        before_dot = self.visit(n.value)

        if isinstance(before_dot, UnboundType) and not before_dot.args:
            return UnboundType("{}.{}".format(before_dot.name, n.attr), line=self.line)
        else:
            self.fail(TYPE_COMMENT_AST_ERROR, self.line, getattr(n, 'col_offset', -1))
            return AnyType(TypeOfAny.from_error)

    # Ellipsis
    def visit_Ellipsis(self, n: ast3_Ellipsis) -> Type:
        return EllipsisType(line=self.line)

    # List(expr* elts, expr_context ctx)
    def visit_List(self, n: ast3.List) -> Type:
        assert isinstance(n.ctx, ast3.Load)
        return self.translate_argument_list(n.elts)


def stringify_name(n: AST) -> Optional[str]:
    if isinstance(n, Name):
        return n.id
    elif isinstance(n, Attribute):
        sv = stringify_name(n.value)
        if sv is not None:
            return "{}.{}".format(sv, n.attr)
    return None  # Can't do it.
