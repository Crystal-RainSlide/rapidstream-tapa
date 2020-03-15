import re
from typing import Iterator, Optional, Tuple, Union

from tlp.verilog import ast

REGISTER_LEVEL = 3


class Pipeline:

  def __init__(self, name: str, width: Optional[int] = None):
    self._ids = tuple(
        ast.Identifier(f'{name}_q%d' % i) for i in range(REGISTER_LEVEL + 1))
    self._width: Optional[ast.Width] = width and ast.make_width(width)

  def __getitem__(self, idx) -> ast.Identifier:
    return self._ids[idx]

  def __iter__(self) -> Iterator[ast.Identifier]:
    return iter(self._ids)

  @property
  def signals(self) -> Iterator[Union[ast.Reg, ast.Wire, ast.Pragma]]:
    yield ast.Wire(name=self[0].name, width=self._width)
    for x in self[1:]:
      yield ast.Pragma(ast.PragmaEntry('dont_touch = "yes"'))
      yield ast.Reg(name=x.name, width=self._width)


def match_fifo_name(name: str) -> Optional[Tuple[str, int]]:
  match = re.fullmatch(r'(\w+)\[(\d+)\]', name)
  if match is not None:
    return match[1], int(match[2])
  return None


def sanitize_fifo_name(name: str) -> str:
  match = match_fifo_name(name)
  if match is not None:
    return f'{match[0]}__{match[1]}'
  return name


def wire_name(fifo: str, suffix: str) -> str:
  """Return the wire name of the fifo signals in generated modules.

  Args:
      fifo (str): Name of the fifo.
      suffix (str): One of the suffixes in ISTREAM_SUFFIXES or OSTREAM_SUFFIXES.

  Returns:
      str: Wire name of the fifo signal.
  """
  fifo = sanitize_fifo_name(fifo)
  if suffix.startswith('_'):
    suffix = suffix[1:]
  return f'{fifo}__{suffix}'