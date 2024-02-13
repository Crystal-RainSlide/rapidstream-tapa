import collections
import itertools
import logging
import os.path
import tempfile
from typing import Callable, Dict, Iterable, Iterator, Optional, Tuple, Union

from pyverilog.ast_code_generator import codegen
from pyverilog.vparser import parser

from tapa.verilog import ast
# pylint: disable=wildcard-import,unused-wildcard-import
from tapa.verilog.util import *
from tapa.verilog.xilinx.async_mmap import *
from tapa.verilog.xilinx.const import *
from tapa.verilog.xilinx.m_axi import *
from tapa.verilog.xilinx.typing import *

_logger = logging.getLogger().getChild(__name__)

__all__ = [
    'Module',
    'generate_m_axi_ports',
]


class Module:
  """AST and helpers for a verilog module.

  `_next_*_idx` is the index to module_def.items where the next type of item
  should be inserted.

  Attributes:
    ast: The ast.Source node.
    directives: Tuple of Directives.
    _handshake_output_ports: A mapping from ap_done, ap_idle, ap_ready signal
        names to their ast.Assign nodes.
    _next_io_port_idx: Next index of an IOPort in module_def.items.
    _next_signal_idx: Next index of ast.Wire or ast.Reg in module_def.items.
    _next_param_idx: Next index of ast.Parameter in module_def.items.
    _next_instance_idx: Next index of ast.InstanceList in module_def.items.
    _next_logic_idx: Next index of ast.Assign or ast.Always in module_def.items.
  """

  # module_def.items should contain the following attributes, in that order.
  _ATTRS = 'param', 'io_port', 'signal', 'logic', 'instance'

  def __init__(self, files: Iterable[str], is_trimming_enabled: bool = False):
    """Construct a Module from files. """
    if not files:
      return
    with tempfile.TemporaryDirectory(prefix='pyverilog-') as output_dir:
      if is_trimming_enabled:
        # trim the body since we only need the interface information
        new_files = []
        for idx, file in enumerate(files):
          lines = []
          with open(file) as fp:
            for line in fp:
              items = line.strip().split()
              if (len(items) > 1 and items[0] in {'reg', 'wire'} and
                  items[1].startswith('ap_rst')):
                lines.append('endmodule')
                break
              lines.append(line)
          new_file = os.path.join(output_dir, f'trimmed_{idx}.v')
          with open(new_file, 'w') as fp:
            fp.writelines(lines)
          new_files.append(new_file)
        files = new_files
      codeparser = parser.VerilogCodeParser(
          files,
          preprocess_output=os.path.join(output_dir, 'preprocess.output'),
          outputdir=output_dir,
          debug=False,
      )
      self.ast: ast.Source = codeparser.parse()
      self.directives: Tuple[Directive, ...] = codeparser.get_directives()
    self._handshake_output_ports: Dict[str, ast.Assign] = {}
    self._calculate_indices()

  def _calculate_indices(self) -> None:
    for idx, item in enumerate(self._module_def.items):
      if isinstance(item, ast.Decl):
        if any(
            isinstance(x, (ast.Input, ast.Output, ast.Input))
            for x in item.list):
          self._next_io_port_idx = idx + 1
        elif any(isinstance(x, (ast.Wire, ast.Reg)) for x in item.list):
          self._next_signal_idx = idx + 1
        elif any(isinstance(x, ast.Parameter) for x in item.list):
          self._next_param_idx = idx + 1
      elif isinstance(item, (ast.Assign, ast.Always)):
        self._next_logic_idx = idx + 1
        if isinstance(item, ast.Assign):
          if isinstance(item.left, ast.Lvalue):
            name = item.left.var.name
          elif isinstance(item.left, ast.Identifier):
            name = item.left.name
          if name in HANDSHAKE_OUTPUT_PORTS:
            self._handshake_output_ports[name] = item
      elif isinstance(item, ast.InstanceList):
        self._next_instance_idx = idx + 1

    # if the item type is not present, set idx to the array bound
    for attr in 'io_port', 'signal', 'param', 'logic', 'instance':
      if not hasattr(self, f'_next_{attr}_idx'):
        setattr(self, f'_next_{attr}_idx', 0)

  @property
  def _module_def(self) -> ast.ModuleDef:
    _module_defs = [
        x for x in self.ast.description.definitions
        if isinstance(x, ast.ModuleDef)
    ]
    if len(_module_defs) != 1:
      raise ValueError('unexpected number of modules')
    return _module_defs[0]

  @property
  def name(self) -> str:
    return self._module_def.name

  @name.setter
  def name(self, name: str) -> None:
    self._module_def.name = name

  @property
  def register_level(self) -> int:
    """The register level of global control signals.

    The minimum register level is 0, which means no additional registers are
    inserted.

    Returns:
        int: N, where any global control signals are registered by N cycles.
    """
    return getattr(self, '_register_level', 0)

  @register_level.setter
  def register_level(self, level: int) -> None:
    self._register_level = level

  @property
  def rst_n_q(self) -> Pipeline:
    return Pipeline(RST_N.name, level=self.register_level)

  def partition_count_of(self, fifo_name: str) -> int:
    """Get the partition count of each FIFO.

    The minimum partition count is 1, which means the FIFO is implemeneted as a
    whole, not many small FIFOs.

    Args:
        fifo_name (str): Name of the FIFO.

    Returns:
        int: N, where this FIFO is partitioned into N small FIFOs.
    """
    return getattr(self, 'fifo_partition_count', {}).get(fifo_name, 1)

  def get_axi_pipeline_level(self, port_name: str) -> int:
    return getattr(self, 'axi_pipeline_level', {}).get(port_name, 0)

  @property
  def ports(self) -> Dict[str, IOPort]:
    port_lists = (
        x.list for x in self._module_def.items if isinstance(x, ast.Decl))
    return collections.OrderedDict(
        (x.name, x)
        for x in itertools.chain.from_iterable(port_lists)
        if isinstance(x, (ast.Input, ast.Output, ast.Inout)))

  def get_port_of(self, fifo: str, suffix: str) -> IOPort:
    """Return the IOPort of the given fifo with the given suffix.

    Args:
      fifo (str): Name of the fifo.
      suffix (str): One of the suffixes in ISTREAM_SUFFIXES or OSTREAM_SUFFIXES.

    Raises:
      ValueError: Module does not have the port.

    Returns:
      IOPort.
    """
    ports = self.ports
    sanitized_fifo = sanitize_array_name(fifo)
    infixes = ('_V', '_r', '_s', '')
    for infix in infixes:
      port = ports.get(f'{sanitized_fifo}{infix}{suffix}')
      if port is not None:
        return port
    # may be a singleton array without the numerical suffix...
    match = match_array_name(fifo)
    if match is not None and match[1] == 0:
      singleton_fifo = match[0]
      for infix in infixes:
        port = ports.get(f'{singleton_fifo}{infix}{suffix}')
        if port is not None:
          _logger.warning('assuming %s is a singleton array', singleton_fifo)
          return port
    raise ValueError(f'module {self.name} does not have port {fifo}.{suffix}')

  def generate_istream_ports(
      self,
      port: str,
      arg: str,
  ) -> Iterator[ast.PortArg]:
    for suffix in ISTREAM_SUFFIXES:
      arg_name = wire_name(arg, suffix)

      # read port
      yield ast.make_port_arg(
          port=self.get_port_of(port, suffix).name,
          arg=arg_name,
      )
      if STREAM_PORT_DIRECTION[suffix] == 'output':
        arg_name = ''

      # peek port
      match = match_array_name(port)
      if match is None:
        peek_port = f'{port}_peek'
      else:
        peek_port = f'{match[0]}_peek[{match[1]}]'
      yield ast.make_port_arg(
          port=self.get_port_of(peek_port, suffix).name,
          arg=arg_name,
      )

  def generate_ostream_ports(
      self,
      port: str,
      arg: str,
  ) -> Iterator[ast.PortArg]:
    for suffix in OSTREAM_SUFFIXES:
      yield ast.make_port_arg(
          port=self.get_port_of(port, suffix).name,
          arg=wire_name(arg, suffix),
      )

  @property
  def signals(self) -> Dict[str, Union[ast.Wire, ast.Reg]]:
    signal_lists = (
        x.list for x in self._module_def.items if isinstance(x, ast.Decl))
    return collections.OrderedDict(
        (x.name, x)
        for x in itertools.chain.from_iterable(signal_lists)
        if isinstance(x, (ast.Wire, ast.Reg)))

  @property
  def params(self) -> Dict[str, ast.Parameter]:
    param_lists = (
        x.list for x in self._module_def.items if isinstance(x, ast.Decl))
    return collections.OrderedDict(
        (x.name, x)
        for x in itertools.chain.from_iterable(param_lists)
        if isinstance(x, ast.Parameter))

  @property
  def code(self) -> str:
    return '\n'.join(directive for _, directive in self.directives
                    ) + codegen.ASTCodeGenerator().visit(self.ast)

  def _increment_idx(self, length: int, target: str) -> None:
    attr_map = {attr: priority for priority, attr in enumerate(self._ATTRS)}
    target_priority = attr_map.get(target)
    if target_priority is None:
      raise ValueError(f'target must be one of {self._ATTRS}')

    # Get the index of the target once, since it could change in the loop.
    target_idx = getattr(self, f'_next_{target}_idx')

    # Increment `_next_*_idx` if it is after `_next_{target}_idx`. If
    # `_next_*_idx` == `_next_{target}_idx`, increment only if `priority` is
    # larger, i.e., `attr` should show up after `target` in `module_def.items`.
    for priority, attr in enumerate(self._ATTRS):
      attr_name = f'_next_{attr}_idx'
      idx = getattr(self, attr_name)
      if (idx, priority) >= (target_idx, target_priority):
        setattr(self, attr_name, idx + length)

  def _filter(self, func: Callable[[ast.Node], bool], target: str) -> None:
    self._module_def.items = tuple(filter(func, self._module_def.items))
    self._calculate_indices()

  def add_ports(self, ports: Iterable[IOPort]) -> 'Module':
    port_tuple = tuple(ports)
    self._module_def.portlist.ports += tuple(
        ast.Port(name=port.name, width=None, dimensions=None, type=None)
        for port in port_tuple)
    self._module_def.items = (self._module_def.items[:self._next_io_port_idx] +
                              port_tuple +
                              self._module_def.items[self._next_io_port_idx:])
    self._increment_idx(len(port_tuple), 'io_port')
    return self

  def add_signals(self, signals: Iterable[Signal]) -> 'Module':
    signal_tuple = tuple(signals)
    self._module_def.items = (self._module_def.items[:self._next_signal_idx] +
                              signal_tuple +
                              self._module_def.items[self._next_signal_idx:])
    self._increment_idx(len(signal_tuple), 'signal')
    return self

  def add_pipeline(self, q: Pipeline, init: ast.Node) -> None:
    """Add registered signals and logics for q initialized by init.

    Args:
        q (Pipeline): The pipelined variable.
        init (ast.Node): Value used to drive the first stage of the pipeline.
    """
    self.add_signals(q.signals)
    logics = [ast.Assign(left=q[0], right=init)]
    if q.level > 0:
      logics.append(
          ast.Always(
              sens_list=CLK_SENS_LIST,
              statement=ast.make_block(
                  ast.NonblockingSubstitution(left=q[i + 1], right=q[i])
                  for i in range(q.level)),
          ))
    self.add_logics(logics)

  def del_signals(self, prefix: str = '', suffix: str = '') -> None:

    def func(item: ast.Node) -> bool:
      if isinstance(item, ast.Decl):
        item = item.list[0]
        if isinstance(item, (ast.Reg, ast.Wire)):
          name: str = item.name
          if name.startswith(prefix) and name.endswith(suffix):
            return False
      return True

    self._filter(func, 'signal')

  def add_params(self, params: Iterable[ast.Parameter]) -> 'Module':
    param_tuple = tuple(params)
    self._module_def.items = (self._module_def.items[:self._next_param_idx] +
                              param_tuple +
                              self._module_def.items[self._next_param_idx:])
    self._increment_idx(len(param_tuple), 'param')
    return self

  def del_params(self, prefix: str = '', suffix: str = '') -> None:

    def func(item: ast.Node) -> bool:
      if isinstance(item, ast.Decl):
        item = item.list[0]
        if isinstance(item, ast.Parameter):
          name: str = item.name
          if name.startswith(prefix) and name.endswith(suffix):
            return False
      return True

    self._filter(func, 'param')

  def add_instancelist(self, item: ast.InstanceList) -> 'Module':
    self._module_def.items = (self._module_def.items[:self._next_instance_idx] +
                              (item,) +
                              self._module_def.items[self._next_instance_idx:])
    self._increment_idx(1, 'instance')
    return self

  def add_instance(
      self,
      module_name: str,
      instance_name: str,
      ports: Iterable[ast.PortArg],
      params: Iterable[ast.ParamArg] = ()
  ) -> 'Module':
    keep_hier_pragma = '(* keep_hierarchy = "yes" *) '
    item = ast.InstanceList(module=keep_hier_pragma + module_name,
                            parameterlist=params,
                            instances=(ast.Instance(module=None,
                                                    name=instance_name,
                                                    parameterlist=None,
                                                    portlist=ports),))
    self.add_instancelist(item)
    return self

  def add_logics(self, logics: Iterable[Logic]) -> 'Module':
    logic_tuple = tuple(logics)
    self._module_def.items = (self._module_def.items[:self._next_logic_idx] +
                              logic_tuple +
                              self._module_def.items[self._next_logic_idx:])
    self._increment_idx(len(logic_tuple), 'logic')
    return self

  def del_logics(self) -> None:

    def func(item: ast.Node) -> bool:
      if isinstance(item, (ast.Assign, ast.Always, ast.Initial)):
        return False
      return True

    self._filter(func, 'param')

  def del_instances(self, prefix: str = '', suffix: str = '') -> None:

    def func(item: ast.Node) -> bool:
      if isinstance(item, ast.InstanceList) and \
          item.module.startswith(prefix) and \
          item.module.endswith(suffix):
        return False
      return True

    self._filter(func, 'instance')

  def del_pragmas(self, pragma: Iterable[str]) -> None:

    def func(item: ast.Node) -> bool:
      if isinstance(item, ast.Pragma):
        if item.entry.name == pragma or item.entry.name in pragma:
          return False
      return True

    self._filter(func, 'signal')

  def add_fifo_instance(
      self,
      name: str,
      width: int,
      depth: int,
      additional_fifo_pipelining: bool,
  ) -> 'Module':
    name = sanitize_array_name(name)
    rst_q = Pipeline(f'{name}__rst', level=self.register_level)
    self.add_pipeline(rst_q, init=ast.Unot(RST_N))

    def ports() -> Iterator[ast.PortArg]:
      yield ast.make_port_arg(port='clk', arg=CLK)
      yield ast.make_port_arg(port='reset', arg=rst_q[-1])
      yield from (
          ast.make_port_arg(port=port_name, arg=wire_name(name, arg_suffix))
          for port_name, arg_suffix in zip(FIFO_READ_PORTS, ISTREAM_SUFFIXES))
      yield ast.make_port_arg(port=FIFO_READ_PORTS[-1], arg=TRUE)
      yield from (
          ast.make_port_arg(port=port_name, arg=wire_name(name, arg_suffix))
          for port_name, arg_suffix in zip(FIFO_WRITE_PORTS, OSTREAM_SUFFIXES))
      yield ast.make_port_arg(port=FIFO_WRITE_PORTS[-1], arg=TRUE)

    partition_count = self.partition_count_of(name)

    # optionally allow additional pipelining if there is enough space
    if additional_fifo_pipelining:
      partition_count = max(2, partition_count)

    module_name = 'fifo'
    level = []
    if partition_count > 1:
      module_name = 'relay_station'
      level.append(
          ast.ParamArg(
              paramname='LEVEL',
              argname=ast.Constant(partition_count),
          ))
    return self.add_instance(
        module_name=module_name,
        instance_name=name,
        ports=ports(),
        params=(
            ast.ParamArg(paramname='DATA_WIDTH', argname=ast.Constant(width)),
            ast.ParamArg(
                paramname='ADDR_WIDTH',
                argname=ast.Constant(max(1, (depth - 1).bit_length())),
            ),
            ast.ParamArg(paramname='DEPTH', argname=ast.Constant(depth)),
            *level,
        ),
    )

  def add_async_mmap_instance(
      self,
      name: str,
      tags: Iterable[str],
      data_width: int,
      addr_width: int = 64,
      buffer_size: Optional[int] = None,
      max_wait_time: int = 3,
      max_burst_len: Optional[int] = None,
      offset_name: str = '',
  ) -> 'Module':
    rst_q = Pipeline(f'{name}__rst', level=self.register_level)
    self.add_pipeline(rst_q, init=ast.Unot(RST_N))

    paramargs = [
        ast.ParamArg(paramname='DataWidth', argname=ast.Constant(data_width)),
        ast.ParamArg(paramname='DataWidthBytesLog',
                     argname=ast.Constant((data_width // 8 - 1).bit_length())),
    ]
    portargs = [
        ast.make_port_arg(port='clk', arg=CLK),
        ast.make_port_arg(port='rst', arg=rst_q[-1]),
    ]
    paramargs.append(
        ast.ParamArg(paramname='AddrWidth', argname=ast.Constant(addr_width)))
    if buffer_size:
      paramargs.extend((ast.ParamArg(paramname='BufferSize',
                                     argname=ast.Constant(buffer_size)),
                        ast.ParamArg(paramname='BufferSizeLog',
                                     argname=ast.Constant(
                                         (buffer_size - 1).bit_length()))))

    max_wait_time = max(1, max_wait_time)
    paramargs.append(
        ast.ParamArg(paramname='WaitTimeWidth',
                     argname=ast.Constant(max_wait_time.bit_length())))
    paramargs.append(
        ast.ParamArg(paramname='MaxWaitTime',
                     argname=ast.Constant(max(1, max_wait_time))))

    # the base address for this memory channel
    portargs.append(ast.make_port_arg(port='offset', arg=offset_name or name))

    if max_burst_len is None:
      # 1KB burst length
      max_burst_len = max(0, 8192 // data_width - 1)
    paramargs.append(
        ast.ParamArg(paramname='BurstLenWidth', argname=ast.Constant(9)))
    paramargs.append(
        ast.ParamArg(paramname='MaxBurstLen',
                     argname=ast.Constant(max_burst_len)))

    for channel, ports in M_AXI_PORTS.items():
      for port, direction in ports:
        portargs.append(
            ast.make_port_arg(port=f'{M_AXI_PREFIX}{channel}{port}',
                              arg=f'{M_AXI_PREFIX}{name}_{channel}{port}'))

    tags = set(tags)
    for tag in ASYNC_MMAP_SUFFIXES:
      for suffix in ASYNC_MMAP_SUFFIXES[tag]:
        if tag in tags:
          arg = async_mmap_arg_name(arg=name, tag=tag, suffix=suffix)
        else:
          if suffix.endswith('_read') or suffix.endswith('_write'):
            arg = "1'b0"
          elif suffix.endswith('_din'):
            arg = "'d0"
          else:
            arg = ''
        portargs.append(ast.make_port_arg(port=tag + suffix, arg=arg))

    return self.add_instance(module_name='async_mmap',
                             instance_name=async_mmap_instance_name(name),
                             ports=portargs,
                             params=paramargs)

  def find_port(self, prefix: str, suffix: str) -> Optional[str]:
    '''Find an IO port with given prefix and suffix in this module.'''
    for port_name in self.ports:
      if port_name.startswith(prefix) and port_name.endswith(suffix):
        return port_name
    return None

  def add_m_axi(
      self,
      name: str,
      data_width: int,
      addr_width: int = 64,
      id_width: Optional[int] = None,
  ) -> 'Module':
    for channel, ports in M_AXI_PORTS.items():
      io_ports = []
      for port, direction in ports:
        io_ports.append((ast.Input if direction == 'input' else ast.Output)(
            name=f'{M_AXI_PREFIX}{name}_{channel}{port}',
            width=get_m_axi_port_width(port, data_width, addr_width, id_width),
        ))
      self.add_ports(io_ports)
    return self

  def cleanup(self) -> None:
    self.del_params(prefix='ap_ST_fsm_state')
    self.del_signals(prefix='ap_CS_fsm')
    self.del_signals(prefix='ap_NS_fsm')
    self.del_signals(suffix='_read')
    self.del_signals(suffix='_write')
    self.del_signals(suffix='_blk_n')
    self.del_signals(suffix='_regslice')
    self.del_signals(prefix='regslice_')
    self.del_signals(HANDSHAKE_RST)
    self.del_signals(HANDSHAKE_DONE)
    self.del_signals(HANDSHAKE_IDLE)
    self.del_signals(HANDSHAKE_READY)
    self.del_logics()
    self.del_instances(suffix='_regslice_both')
    self.del_pragmas('fsm_encoding')
    self.add_signals(
        map(ast.Wire,
            (HANDSHAKE_RST, HANDSHAKE_DONE, HANDSHAKE_IDLE, HANDSHAKE_READY)))
    self.add_pipeline(self.rst_n_q, init=RST_N)
    self.add_logics([ast.Assign(left=RST, right=ast.Unot(self.rst_n_q[-1]))])

  def _get_nodes_of_type(self, node, *target_types) -> Iterator:
    if isinstance(node, target_types):
      yield node
    for c in node.children():
      yield from self._get_nodes_of_type(c, *target_types)

  def get_nodes_of_type(self, *target_types) -> Iterator:
    yield from self._get_nodes_of_type(self.ast, *target_types)


def generate_m_axi_ports(
    module: Module,
    port: str,
    arg: str,
    arg_reg: str = '',
) -> Iterator[ast.PortArg]:
  """Generate AXI mmap ports that instantiate given module.

  Args:
      module (Module): Module that needs to be instantiated.
      port (str): Port name in the instantiated module.
      arg (str): Argument name in the instantiating module.
      arg_reg (str, optional): Registered argument name. Defaults to ''.

  Raises:
      ValueError: If the offset port cannot be found in the instantiated module.

  Yields:
      Iterator[ast.PortArg]: PortArgs.
  """
  for suffix in M_AXI_SUFFIXES:
    yield ast.make_port_arg(port=M_AXI_PREFIX + port + suffix,
                            arg=M_AXI_PREFIX + arg + suffix)
  for suffix in '_offset', '_data_V', '_V', '':
    port_name = module.find_port(prefix=port, suffix=suffix)
    if port_name is not None:
      if port_name != port + suffix:
        _logger.warn(
            "unexpected offset port `%s' in module"
            " `%s'; please double check if this is the "
            "offset port for m_axi port `%s'", port_name, module.name, port)
      yield ast.make_port_arg(port=port_name, arg=arg_reg or arg)
      break
  else:
    raise ValueError(f'cannot find offset port for {port}')
