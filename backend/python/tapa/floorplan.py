import itertools
import json
import logging
import os
import sys
from collections import defaultdict
from concurrent import futures
from typing import Optional, Dict, Callable, List, Tuple, TextIO

from haoda.report.xilinx import rtl as report
from tapa import util
from tapa.task import Task

from .task_graph import get_edges, get_vertices
from .hardware import get_ctrl_instance_region, get_port_region

from autobridge.main import annotate_floorplan

_logger = logging.getLogger().getChild(__name__)

class InputError(Exception):
  pass


def generate_floorplan(
    part_num: str,
    physical_connectivity: TextIO,
    enable_synth_util: bool,
    user_floorplan_pre_assignments: Optional[TextIO],
    rtl_dir: str,
    top_task: Task,
    post_syn_rpt_getter: Callable[[str], str],
    task_getter: Callable[[str], Task],
    fifo_width_getter: Callable[[Task, str], int],
    cpp_getter: Callable[[str], str],
    **kwargs,
) -> Tuple[Dict, Dict]:
  """
  get the target region of each vertex
  get the pipeline level of each edge
  """
  # run logic synthesis to get an accurate area estimation
  if enable_synth_util:
    get_post_synth_area(
      rtl_dir,
      part_num,
      top_task,
      post_syn_rpt_getter,
      task_getter,
      cpp_getter,
    )

  config = get_floorplan_config(
    part_num,
    physical_connectivity,
    top_task,
    fifo_width_getter,
    user_floorplan_pre_assignments,
    **kwargs,
  )

  config_with_floorplan = annotate_floorplan(config)

  return config, config_with_floorplan


def get_floorplan_result(
    work_dir: str,
    constraint: TextIO,
    reuse_hbm_path_pipelining: bool,
    manual_vivado_flow: bool,
) -> Tuple[Dict[str, str], Dict[str, int]]:
  """ extract floorplan results from the checkpointed config file """
  try:
    config_with_floorplan = json.loads(open(f'{work_dir}/post-floorplan-config.json', 'r').read())
  except:
    raise FileNotFoundError(f'no valid floorplanning results found in work directory {work_dir}')

  # generate the constraint file
  vivado_tcl = get_vivado_tcl(config_with_floorplan, work_dir, reuse_hbm_path_pipelining, manual_vivado_flow)
  constraint.write('\n'.join(vivado_tcl))

  fifo_pipeline_level, axi_pipeline_level = extract_pipeline_level(config_with_floorplan)

  return fifo_pipeline_level, axi_pipeline_level


def extract_pipeline_level(
  config_with_floorplan,
) -> Tuple[Dict[str, str], Dict[str, int]]:
  """ extract the pipeline level of fifos and axi edges """
  if config_with_floorplan.get('floorplan_status') == 'FAILED':
    return {}, {}

  fifo_pipeline_level = {}
  for edge, properties in config_with_floorplan['edges'].items():
    if properties['category'] == 'FIFO_EDGE':
      fifo_pipeline_level[properties['instance']] = len(properties['path'])

  axi_pipeline_level = {}
  for edge, properties in config_with_floorplan['edges'].items():
    if properties['category'] == 'AXI_EDGE':
      # if the AXI module is at the same region as the external port, then no pipelining
      level = len(properties['path']) - 1

      axi_pipeline_level[properties['port_name']] = level

  return fifo_pipeline_level, axi_pipeline_level


def get_vivado_tcl(config_with_floorplan, work_dir, reuse_hbm_path_pipelining, manual_vivado_flow):
  if config_with_floorplan.get('floorplan_status') == 'FAILED':
    return ['# Floorplan failed']

  vivado_tcl = []

  vivado_tcl.append('puts "applying partitioning constraints generated by tapac"')
  vivado_tcl.append('write_checkpoint before_add_floorplan_constraints.dcp')

  # pblock definitions
  vivado_tcl += list(config_with_floorplan['floorplan_region_pblock_tcl'].values())

  # floorplan vertices
  region_to_inst = defaultdict(list)
  for vertex, properties in config_with_floorplan['vertices'].items():
    if properties['category'] == 'PORT_VERTEX':
      continue
    region = properties['floorplan_region']
    inst = properties['instance']
    region_to_inst[region].append(inst)

  # floorplan pipeline registers
  for edge, properties in config_with_floorplan['edges'].items():
    if properties['category'] == 'FIFO_EDGE':
      fifo_name = properties['instance']
      path = properties['path']
      if len(path) > 1:
        for i in range(len(path)):
          region_to_inst[path[i]].append(f'{fifo_name}/inst\\\\[{i}]\\\\.unit')
      else:
        region_to_inst[path[0]].append(f'{fifo_name}/.*.unit')

  # print out the constraints
  for region, inst_list in region_to_inst.items():
    vivado_tcl.append(f'add_cells_to_pblock [get_pblocks {region}] [get_cells -regex {{')
    vivado_tcl += [f'  pfm_top_i/dynamic_region/.*/inst/.*/{inst}' for inst in inst_list]
    vivado_tcl.append(f'}} ]')

  # redundant clean up code for extra safety
  vivado_tcl.append('foreach pblock [get_pblocks -regexp CR_X\\\\d+Y\\\\d+_To_CR_X\\\\d+Y\\\\d+] {')
  vivado_tcl.append('  if {[get_property CELL_COUNT $pblock] == 0} {')
  vivado_tcl.append('    puts "WARNING: delete empty pblock $pblock "')
  vivado_tcl.append('    delete_pblocks $pblock')
  vivado_tcl.append('  }')
  vivado_tcl.append('}')

  vivado_tcl.append('foreach pblock [get_pblocks] {')
  vivado_tcl.append(f'  report_utilization -pblocks $pblock -file {work_dir}/report/$pblock.rpt')
  vivado_tcl.append('}',)

  if reuse_hbm_path_pipelining:
    vivado_tcl.append('')
    vivado_tcl.append('# remove the pblock of hbm paths')
    vivado_tcl.append('for {set i 0} {$i < 32} {incr i} {')
    vivado_tcl.append('  add_cells_to_pblock -quiet pblock_dynamic_region [get_cells [list pfm_top_i/dynamic_region/hmss_0/inst/path_${i}]] -clear_locs')
    vivado_tcl.append('}')

  if manual_vivado_flow:
    vivado_tcl.append('')
    vivado_tcl.append('opt_design -directive Explore')
    vivado_tcl.append('place_design -directive EarlyBlockPlacement -retiming')
    # two passes of phys_opt_design after placement
    vivado_tcl.append('phys_opt_design -directive Explore')
    vivado_tcl.append('phys_opt_design -directive Explore')
    vivado_tcl.append('write_checkpoint place_opt.dcp')
    vivado_tcl.append('route_design -directive Explore')
    vivado_tcl.append('write_checkpoint route.dcp')
    vivado_tcl.append('phys_opt_design -directive Explore')
    vivado_tcl.append('write_checkpoint route_opt.dcp')
    vivado_tcl.append('exit')

  return vivado_tcl


def checkpoint_floorplan(config_with_floorplan, work_dir):
  """ Save a copy of the region -> instances into a json file
  """
  if config_with_floorplan.get('floorplan_status') == 'FAILED':
    _logger.warning('failed to get a valid floorplanning')
    return

  region_to_inst = defaultdict(list)
  for vertex, properties in config_with_floorplan['vertices'].items():
    if properties['category'] == 'PORT_VERTEX':
      continue
    region = properties['floorplan_region']
    region_to_inst[region].append(vertex)

  open(f'{work_dir}/floorplan-region-to-instances.json', 'w').write(
    json.dumps(region_to_inst, indent=2)
  )


def get_floorplan_config(
    part_num: str,
    physical_connectivity: TextIO,
    top_task: Task,
    fifo_width_getter: Callable[[Task, str], int],
    user_floorplan_pre_assignments: Optional[TextIO],
    **kwargs,
) -> Dict:
  """ Generate a json encoding the task graph for the floorplanner
  """
  arg_name_to_external_port = util.parse_connectivity_and_check_completeness(
                                physical_connectivity,
                                top_task,
                              )

  edges = get_edges(top_task, fifo_width_getter)
  vertices = get_vertices(top_task, arg_name_to_external_port)
  floorplan_pre_assignments = get_floorplan_pre_assignments(
                                part_num,
                                user_floorplan_pre_assignments,
                                vertices,
                              )

  grouping_constraints = get_grouping_constraints(edges)

  config = {
    'part_num': part_num,
    'edges': edges,
    'vertices': vertices,
    'floorplan_pre_assignments': floorplan_pre_assignments,
    'grouping_constraints': grouping_constraints,
  }

  config.update(kwargs)

  return config


def get_grouping_constraints(edges: Dict) -> List[List[str]]:
  """ specify which tasks must be placed in the same slot """
  grouping = []
  for edge, properties in edges.items():
    if properties['category'] == 'ASYNC_MMAP_EDGE':
      grouping.append([properties['produced_by'], properties['consumed_by']])
  return grouping


def get_floorplan_pre_assignments(
    part_num: str,
    user_floorplan_pre_assignments_io: Optional[TextIO],
    vertices: Dict[str, Dict]
) -> Dict[str, List[str]]:
  """ constraints of which modules must be assigned to which slots
      including user pre-assignments and system ones
      the s_axi_constrol is always assigned to the bottom left slot in U250/U280
  """
  floorplan_pre_assignments = defaultdict(list)

  # user pre assignment
  if user_floorplan_pre_assignments_io:
    user_pre_assignments = json.load(user_floorplan_pre_assignments_io)
    for region, modules in user_pre_assignments.items():
      floorplan_pre_assignments[region] += modules

  # ctrl vertex pre assignment
  ctrl_region = get_ctrl_instance_region(part_num)
  ctrl_vertex = [v_name for v_name, properties in vertices.items() if properties['category'] == 'CTRL_VERTEX']
  assert len(ctrl_vertex) == 1, f'more than 1 ctrl instance found: {ctrl_vertex}'
  floorplan_pre_assignments[ctrl_region] += ctrl_vertex

  # port vertices pre assignment
  for v_name, properties in vertices.items():
    if properties['category'] == 'PORT_VERTEX':
      region = get_port_region(part_num, properties['port_cat'], properties['port_id'])
      floorplan_pre_assignments[region].append(v_name)

  return floorplan_pre_assignments


def get_post_synth_area(
    rtl_dir,
    part_num,
    top_task,
    post_syn_rpt_getter: Callable[[str], str],
    task_getter: Callable[[str], Task],
    cpp_getter: Callable[[str], str],
):
  def worker(module_name: str, idx: int) -> report.HierarchicalUtilization:
    _logger.debug('synthesizing %s', module_name)
    rpt_path = post_syn_rpt_getter(module_name)

    rpt_path_mtime = 0.
    if os.path.isfile(rpt_path):
      rpt_path_mtime = os.path.getmtime(rpt_path)

    # generate report if and only if C++ source is newer than report.
    if os.path.getmtime(cpp_getter(module_name)) > rpt_path_mtime:
      os.nice(idx % 19)
      with report.ReportDirUtil(
          rtl_dir,
          rpt_path,
          module_name,
          part_num,
          synth_kwargs={'mode': 'out_of_context'},
      ) as proc:
        stdout, stderr = proc.communicate()

      # err if output report does not exist or is not newer than previous
      if (not os.path.isfile(rpt_path) or
          os.path.getmtime(rpt_path) <= rpt_path_mtime):
        sys.stdout.write(stdout.decode('utf-8'))
        sys.stderr.write(stderr.decode('utf-8'))
        raise InputError(f'failed to generate report for {module_name}')

    with open(rpt_path) as rpt_file:
      return report.parse_hierarchical_utilization_report(rpt_file)

  _logger.info('generating post-synthesis resource utilization reports')
  _logger.info('this step runs logic synthesis of each task for accurate area info, it may take a while')
  with futures.ThreadPoolExecutor(max_workers=util.nproc()) as executor:
    for utilization in executor.map(
        worker,
        {x.task.name for x in top_task.instances},
        itertools.count(0),
    ):
      # override self_area populated from HLS report
      bram = int(utilization['RAMB36']) * 2 + int(utilization['RAMB18'])
      task_getter(utilization.instance).total_area = {
          'BRAM_18K': bram,
          'DSP': int(utilization['DSP Blocks']),
          'FF': int(utilization['FFs']),
          'LUT': int(utilization['Total LUTs']),
          'URAM': int(utilization['URAM']),
      }
