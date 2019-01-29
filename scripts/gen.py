#!/usr/bin/python

from __future__ import print_function

import argparse
import collections
import lark
import os
import re
import sys

FuncGen = collections.namedtuple(
    'FuncGen',
    'tree, xtree, rwxtree, func, xfunc, code, sig, rwsig, cppsig, funsig, mapsig'
)

_GRAMMAR = r"""
    start: type fnname "(" params ")"
    type: CONST? core_type refspec?
    fnname: CNAME
    refspec: REF
           | PTR
    core_type: template
        | TNAME
    template: TNAME "<" typelist ">"
    typelist: type
            | type "," typelist
    REF: "&"
    PTR: "*"
    CONST: "const"
    TNAME: /[a-zA-Z0-9_:]+/
    params: param
          | param "," params
    param: type param_name
    param_name: CNAME

    %import common.CNAME -> CNAME
    %import common.WS
    %ignore WS
    """

_PARSER = lark.Lark(_GRAMMAR, parser='lalr', propagate_positions=True)

_XPARSER = lark.Lark(
    _GRAMMAR, parser='lalr', propagate_positions=True, keep_all_tokens=True)

_FN_BLACKLIST = set([
    # ATEN functions
    'toBackend',
    'toScalarType',
    'copy',
    'copy_',
    'backward',
    'set_data',
    'tensorFromBlob',
    'tensorWithAllocator',
    'storageFromBlob',
    'storageWithAllocator',
    'unsafeStorageFromTH',
    'unsafeTensorFromTH',
    # XLA/TPU functions
])

_FN_BLACKLIST_REGEX = [
    # ATEN functions
    r'.*cudnn',
    # XLA/TPU functions
]

_TYPE_NSMAP = {
    'Tensor': 'at::Tensor',
    'TensorList': 'at::TensorList',
    'Scalar': 'at::Scalar',
    'Storage': 'at::Storage',
    'IntList': 'at::IntList',
    'Generator': 'at::Generator',
    'ScalarType': 'at::ScalarType',
    'TensorOptions': 'at::TensorOptions',
    'SparseTensorRef': 'at::SparseTensorRef',
    'Device': 'c10::Device',
}

_CPP_HEADER = """// Autogenerated file by {gen}. Do not edit directly!

#include "aten_xla_bridge.h"

namespace torch_xla {{
namespace {{

{funcs}
}}  // namespace

{regs}
}}  // namespace torch_xla
"""

_CPP_CLASS_HEADER = """// Autogenerated file by {gen}. Do not edit directly!

#include "aten_xla_bridge.h"

#include <ATen/Context.h>
#include <ATen/CPUGenerator.h>
#include <ATen/TypeDefault.h>

namespace torch_xla {{

class XLATensorType : public at::TypeDefault {{
 public:
  XLATensorType(at::TensorTypeId type_id, bool is_variable, bool is_undefined)
    : at::TypeDefault(type_id, is_variable, is_undefined) {{}}

  // A serie of hacks follows!
  at::ScalarType scalarType() const override {{
    return at::ScalarType::Undefined;
  }}

  caffe2::TypeMeta typeMeta() const override {{
    return scalarTypeToTypeMeta(scalarType());
  }}

  at::Backend backend() const override {{
    return at::Backend::Undefined;
  }}

  at::Allocator * allocator() const override {{
    return at::getCPUAllocator();
  }}

  c10::Device getDeviceFromPtr(void * data) const override {{
    return at::DeviceType::CPU;
  }}

  std::unique_ptr<at::Generator> generator() const override {{
    return std::unique_ptr<at::Generator>(new at::CPUGenerator(&at::globalContext()));
  }}

  const char * toString() const override {{
    return "XLATensorType";
  }}

  size_t elementSizeInBytes() const override {{
    return 4;
  }}

  at::TypeID ID() const override {{
    return at::TypeID::Undefined;
  }}

{hfuncs}
}};

{funcs}
at::Type* GetXLATensorType() {{
  static XLATensorType* xla_type = new XLATensorType(
    c10::UndefinedTensorId(), /*is_variable=*/false, /*is_undefined=*/false);
  return xla_type;
}}

}}  // namespace torch_xla
"""

_RESULT_NAME = 'x_result'


class Context(object):

  def __init__(self, functions, native_functions, generate_class):
    self.generate_class = generate_class
    self.defdb = {}
    with open(functions, 'r') as ff:
      self.functions_data = ff.read()
    with open(native_functions, 'r') as ff:
      self.native_functions_data = ff.read()

  def get_function(self, name, ref_param):
    if self.functions_data.find(' {}('.format(name)) >= 0:
      return 'at::{}'.format(name)
    if self.native_functions_data.find(' {}('.format(name)) >= 0:
      return 'at::native::{}'.format(name)
    return 'at::detail::infer_type({}).{}'.format(ref_param, name)


class StringEmit(object):

  def __init__(self, sref):
    self.sref = sref
    self.sval = ''
    self.pos = -1

  def __repr__(self):
    return self.sval

  def advance(self, t):
    start = t.column - 1
    end = t.end_column - 1
    pos = self.pos if self.pos >= 0 else start
    if start > pos:
      self.sval += self.sref[pos:start]
    self.sval += t.value
    self.pos = end

  def skip(self, t):
    self.pos = last_match(t) if self.pos >= 0 else -1

  def append(self, s):
    self.sval += s
    self.pos = -1


def list_get(l, n):
  return l[n] if n < len(l) else None


def is_blacklisted_fn(fname):
  if fname in _FN_BLACKLIST:
    return True
  for frx in _FN_BLACKLIST_REGEX:
    if re.match(frx, fname):
      return True
  return False


def first_match(t):
  if isinstance(t, lark.lexer.Token):
    return t.column - 1
  assert isinstance(t, lark.tree.Tree)
  return first_match(t.children[0])


def last_match(t):
  if isinstance(t, lark.lexer.Token):
    return t.end_column - 1
  assert isinstance(t, lark.tree.Tree)
  return last_match(t.children[-1])


def for_every_token(t, fn):
  if isinstance(t, lark.lexer.Token):
    fn(t)
  else:
    assert isinstance(t, lark.tree.Tree)
    for c in t.children:
      for_every_token(c, fn)


def emit_string(t, emit, emit_fn):
  status = emit_fn(t)
  if status > 0:

    def do_emit(tok):
      emit.advance(tok)

    for_every_token(t, do_emit)
  elif status == 0:
    if isinstance(t, lark.lexer.Token):
      emit.advance(t)
    else:
      assert isinstance(t, lark.tree.Tree)
      for c in t.children:
        emit_string(c, emit, emit_fn)
  else:
    emit.skip(t)


def typed_child(t, n, ttype):
  assert isinstance(t, lark.tree.Tree)
  assert n < len(t.children)
  c = t.children[n]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == ttype, t.pretty()
  return c


def rewrite_sig(tree, orig_sig):
  emit = StringEmit(orig_sig)
  emit_string(tree, emit, lambda t: 0)
  return str(emit)


def rewrite_types(sig, tmap):

  def rewrite(t):
    if t.type == 'TNAME':
      new_type = tmap.get(t.value, None)
      if new_type is not None:
        t.value = new_type

  xtree = _XPARSER.parse(sig)
  for_every_token(xtree, rewrite)
  return rewrite_sig(xtree, sig)


def create_stdfunc_sig(tree, orig_sig):

  def emit_fn(t):
    if isinstance(t, lark.lexer.Token):
      return 0
    return -1 if t.data == 'param_name' else 0

  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(tree, 0, 'type'), emit, emit_fn)
  emit.append('(')
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(tree, 3, 'params'), emit, emit_fn)
  emit.append(')')
  return str(emit)


def create_map_sig(tree, orig_sig):

  def emit_fn(t):
    if isinstance(t, lark.lexer.Token):
      return -1 if t.type in ['CONST', 'REF', 'PTR'] else 0
    return -1 if t.data == 'param_name' else 0

  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(tree, 1, 'fnname'), emit, emit_fn)
  emit.append('(')
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(tree, 3, 'params'), emit, emit_fn)
  emit.append(') -> ')
  emit_string(typed_child(tree, 0, 'type'), emit, emit_fn)
  return str(emit)


def type_core(t):
  assert isinstance(t, lark.tree.Tree)
  for c in t.children:
    if isinstance(c, lark.tree.Tree) and c.data == 'core_type':
      c = c.children[0]
      if isinstance(c, lark.lexer.Token):
        return c.value
      assert isinstance(c, lark.tree.Tree) and c.data == 'template'
      return c.children[0].value
  raise RuntimeError('Not a type tree: {}'.format(t))


def type_is_const(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[0]
  return isinstance(c, lark.lexer.Token) and c.value == 'const'


def type_is_refptr(t, kind):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[-1]
  if not isinstance(c, lark.tree.Tree) or c.data != 'refspec':
    return False
  c = c.children[0]
  return isinstance(c, lark.lexer.Token) and c.value == kind


def extract_list(t, l):
  assert isinstance(t, lark.tree.Tree)
  l.append(t.children[0])
  if len(t.children) == 2:
    c = t.children[1]
    if isinstance(c, lark.tree.Tree) and c.data == t.data:
      extract_list(c, l)
  return l


def tuple_type_list(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[0]
  assert isinstance(c, lark.tree.Tree) and c.data == 'core_type'
  c = c.children[0]
  assert isinstance(c, lark.tree.Tree) and c.data == 'template'
  types = []
  return extract_list(c.children[1], types)


def get_function_name(t):
  assert isinstance(t, lark.tree.Tree)
  fname = t.children[1]
  assert isinstance(fname, lark.tree.Tree)
  assert fname.data == 'fnname'
  return fname.children[0].value


def get_function_signature(t, orig_sig, namefn):
  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(t, 0, 'type'), emit, lambda t: 0)
  fnname = typed_child(t, 1, 'fnname').children[0]
  xfname = namefn(fnname.value)
  emit.append(' {}('.format(xfname))
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(t, 3, 'params'), emit, lambda t: 0)
  emit.append(')')
  return str(emit), fnname.value, xfname


def get_parameters(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[2]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == 'params'
  params = []
  extract_list(c, params)
  return params


def param_name(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[1]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == 'param_name'
  token = c.children[0]
  assert isinstance(token, lark.lexer.Token)
  return token.value


def get_return_value(rtype, rname, param, var, ref_param):
  crtype = type_core(rtype)
  if type_is_const(rtype) or type_is_refptr(rtype, '&'):
    # If the return type is a const or a reference, return the matching
    # parameter. In these cases we operated on XLA tensors data (the ATEN one),
    # but the returned references are the input parameters.
    assert param
    return param_name(param)
  elif crtype != 'Tensor':
    return rname
  else:
    # If instead the return type is a value Tensor, we create a new one by
    # wrapping the proper local variable which has been created by calling
    # into the CPU tensor implementation.
    return 'bridge::CreateXlaTensor({}, bridge::XlaTensorDevice({}))'.format(
        rname, ref_param or param_name(param))


def get_reference_param(params):
  # The reference parameter is the Tensor object which we use to extract the
  # result Tensor device, if any.
  ref_param = None
  other = None
  for p in params:
    ptype = p.children[0]
    cptype = type_core(ptype)
    pname = param_name(p)
    if cptype == 'TensorOptions' or cptype == 'TensorList':
      other = pname
    if cptype != 'Tensor':
      continue
    if pname == 'self':
      return pname
    if type_is_const(ptype):
      ref_param = pname
  return ref_param or other


def get_tuple_return(rtype, rtype_str, rname, params, param_vars, ref_param):
  types = tuple_type_list(rtype)
  retstr = '{}('.format(rtype_str)
  for i, ttype in enumerate(types):
    if i > 0:
      retstr += ', '
    tuple_var = 'std::get<{}>({})'.format(i, rname)
    retstr += get_return_value(ttype, tuple_var, list_get(params, i),
                               list_get(param_vars, i), ref_param)
  return retstr + ')'


def get_return_type_str(t, orig_sig):
  assert isinstance(t, lark.tree.Tree)
  fname = t.children[1]
  assert isinstance(fname, lark.tree.Tree)
  assert fname.data == 'fnname'
  token = fname.children[0]
  assert isinstance(token, lark.lexer.Token)
  return orig_sig[0:token.column - 2]


def generate_return_stmt(t, rtype_str, fname, rname, params, param_vars,
                         ref_param):
  assert isinstance(t, lark.tree.Tree)
  rtype = t.children[0]
  ctype = type_core(rtype)
  if ctype == 'std::tuple':
    retstr = get_tuple_return(rtype, rtype_str, rname, params, param_vars,
                              ref_param)
  elif ctype == 'std::vector':
    retstr = 'bridge::CreateXlaTensors({}, bridge::XlaTensorDevice({}))'.format(
        rname, ref_param)
  elif ctype == 'Tensor':
    retstr = get_return_value(rtype, rname, params[0], param_vars[0], ref_param)
  elif ctype == 'void' and not type_is_refptr(rtype, '*'):
    return ''
  else:
    retstr = rname
  return '  return {};\n'.format(retstr)


def generate_result_assignment(t, rname):
  assert isinstance(t, lark.tree.Tree)
  rtype = t.children[0]
  ctype = type_core(rtype)
  if ctype == 'void' and not type_is_refptr(rtype, '*'):
    return ''
  return 'auto&& {} = '.format(rname)


def get_xla_wrapper(orig_sig, ctx):
  tree = _PARSER.parse(orig_sig)
  xtree = _XPARSER.parse(orig_sig)
  rwsig = rewrite_types(orig_sig, _TYPE_NSMAP)
  rwxtree = _XPARSER.parse(rwsig)
  params = get_parameters(tree)
  ref_param = get_reference_param(params)

  # There are a few functions with the same function name but different
  # parameter list. Generate a unique XL function name here.
  def gen_fnname(x):
    if ctx.generate_class:
      return 'XLATensorType::{}'.format(x)
    post = ''
    if x in ctx.defdb:
      post = '_{}'.format(ctx.defdb[x])
      ctx.defdb[x] += 1
    else:
      ctx.defdb[x] = 1
    return 'xla_' + x + post

  sig, fname, xfname = get_function_signature(rwxtree, rwsig, gen_fnname)
  code = '{} {}{{\n'.format(sig, 'const ' if ctx.generate_class else '')
  xla_ref_param = ref_param
  param_vars = []
  for p in params:
    ptype = p.children[0]
    cptype = type_core(ptype)
    pname = param_name(p)
    if cptype == 'TensorList':
      xname = 'l_{}'.format(pname)
      code += '  auto {} = bridge::XlaCreateTensorList({});\n'.format(
          xname, pname)
      param_vars.append(xname)
    elif cptype != 'Tensor':
      param_vars.append(pname)
    elif type_is_const(ptype):
      xname = 'r_{}'.format(pname)
      code += '  auto {} = bridge::XlaToAtenTensor({});\n'.format(xname, pname)
      param_vars.append(xname)
    else:
      xname = 'w_{}'.format(pname)
      code += '  auto {} = bridge::XlaToAtenMutableTensor({});\n'.format(
          xname, pname)
      param_vars.append(xname)
    if pname == ref_param:
      xla_ref_param = param_vars[-1]
  result_assign = generate_result_assignment(tree, _RESULT_NAME)
  code += '  {}{}('.format(result_assign, ctx.get_function(
      fname, xla_ref_param))
  for i, v in enumerate(param_vars):
    if i > 0:
      code += ', '
    code += v
  code += ');\n'
  if result_assign:
    code += ('  static_cast<void>({}); // Avoid warnings in case not '
             'used\n'.format(_RESULT_NAME))
  code += generate_return_stmt(tree, get_return_type_str(rwxtree, rwsig), fname,
                               _RESULT_NAME if result_assign else None, params,
                               param_vars, ref_param)
  code += '}'
  return FuncGen(
      tree=tree,
      xtree=xtree,
      rwxtree=rwxtree,
      func=fname,
      xfunc=xfname,
      code=code,
      sig=orig_sig,
      rwsig=rwsig,
      cppsig=sig,
      funsig=create_stdfunc_sig(rwxtree, rwsig),
      mapsig=create_map_sig(xtree, orig_sig))


def extract_functions(path):
  functions = []
  for line in open(path, 'r'):
    m = re.match(r'\s*([^\s].*) const override;', line)
    if not m:
      continue
    fndef = m.group(1)
    try:
      tree = _PARSER.parse(fndef)
      fname = get_function_name(tree)
      if not is_blacklisted_fn(fname):
        functions.append(fndef)
    except:
      pass
  return functions


def generate_registrations(fgens):
  code = 'void RegisterAtenTypeFunctions() {\n'
  for fgen in fgens:
    code += (
        '  at::register_extension_backend_op(\n    Backend::XLA,\n    "{}",\n'
        '    &{});\n'.format(fgen.mapsig, fgen.xfunc))
  return code + '}\n'


def generate_functions(fgens):
  code = ''
  for fgen in fgens:
    code += '{}\n\n'.format(fgen.code)
  return code


def generate_class_functions(fgens):
  code = ''
  for fgen in fgens:
    code += '  {} const override;\n'.format(fgen.rwsig)
  return code


def generate(args):
  ofile = sys.stdout
  if args.output:
    ofile = open(args.output, 'w')

  fndefs = extract_functions(args.typedef)
  print(
      'Extracted {} functions from {}'.format(len(fndefs), args.typedef),
      file=sys.stderr)
  fgens = []
  ctx = Context(args.functions, args.native_functions, args.generate_class)
  for ts in fndefs:
    fgens.append(get_xla_wrapper(ts, ctx))

  functions = generate_functions(fgens)
  if args.generate_class:
    hfunctions = generate_class_functions(fgens)
    print(
        _CPP_CLASS_HEADER.format(
            gen=os.path.basename(sys.argv[0]),
            funcs=functions,
            hfuncs=hfunctions),
        file=ofile)
  else:
    regs = generate_registrations(fgens)
    print(
        _CPP_HEADER.format(
            gen=os.path.basename(sys.argv[0]), funcs=functions, regs=regs),
        file=ofile)


if __name__ == '__main__':
  arg_parser = argparse.ArgumentParser()
  arg_parser.add_argument('--output', type=str)
  arg_parser.add_argument('--generate_class', action='store_true')
  arg_parser.add_argument(
      'typedef',
      type=str,
      metavar='TYPE_DEFAULT_FILE',
      help='The path to the TypeDefault.h file')
  arg_parser.add_argument(
      'functions',
      type=str,
      metavar='FUNCTIONS_FILE',
      help='The path to the Functions.h file')
  arg_parser.add_argument(
      'native_functions',
      type=str,
      metavar='NATIVE_FUNCTIONS_FILE',
      help='The path to the NativeFunctions.h file')
  args, files = arg_parser.parse_known_args()
  generate(args)
