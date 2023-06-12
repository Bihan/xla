import os
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
import torch
import torch_xla
import torch_xla.core.xla_model as xm
from torch_xla.experimental.xla_sharded_tensor import XLAShardedTensor, XLAShard
import torch_xla.runtime as xr

import numpy as np
import itertools
from typing import Tuple, Union, List, Sequence, Any, Optional
from enum import IntEnum


class Mesh:
  """Describe the logical XLA device topology mesh and the underlying resources.

  Args:
    device_ids (Union[np.ndarray, List]): A raveled list of devices (IDs) in a custom order. The list is reshaped
        to an `mesh_shape` array, filling the elements using C-like index order.

    mesh_shape (Tuple[int, ...]): A int tuple describing the logical topology shape
        of the device mesh, and each element describes the number of devices in
        the corresponding axis.

    axis_names (Tuple[str, ...]): A sequence of resource axis names to be assigned to the dimensions
        of the `devices` argument. Its length should match the rank of `devices`.

  Example:
  —------------------------------
  mesh_shape = (4, 2)
  num_devices = len(xm.get_xla_supported_devices())
  device_ids = np.array(range(num_devices))
  mesh = Mesh(device_ids, mesh_shape, ('x', 'y'))
  mesh.get_logical_mesh()
  >> array([[0, 1],
            [2, 3],
            [4, 5],
            [6, 7]])
  mesh.shape()
  >> OrderedDict([('x', 4), ('y', 2)])
  """

  device_ids: np.ndarray
  mesh_shape: Tuple[int, ...]
  axis_names: Tuple[str, ...]

  def __init__(self,
               device_ids: Union[np.ndarray, List],
               mesh_shape: Tuple[int, ...],
               axis_names: Tuple[str, ...] = None):
    if not isinstance(device_ids, np.ndarray):
      device_ids = np.array(device_ids)
    assert (axis_names is None) or (len(mesh_shape) == len(axis_names))
    assert (len(device_ids) == np.prod(mesh_shape))
    assert len(device_ids) == len(np.unique(device_ids))
    self.device_ids = device_ids
    self.mesh_shape = mesh_shape
    self.axis_names = axis_names
    assert all(d < self.size() for d in device_ids)

  def size(self):
    return np.prod(self.mesh_shape)

  def shape(self):
    return OrderedDict(
        (name, size) for name, size in zip(self.axis_name, self.mesh_shape))

  def get_logical_mesh(self):
    return self.device_ids.reshape(self.mesh_shape)


class HybridMesh(Mesh):
  ici_mesh_shape: Tuple[int, ...]
  dcn_mesh_shape: Tuple[int, ...]

  def __init__(self,
               *,
               ici_mesh_shape: Tuple[int, ...],
               dcn_mesh_shape: Tuple[int, ...] = None,
               axis_names: Tuple[str, ...] = None):
    if dcn_mesh_shape == None:
      dcn_mesh_shape = tuple([1] * len(ici_mesh_shape))
    assert len(ici_mesh_shape) == len(dcn_mesh_shape)
    mesh_shape = tuple([x * y for x, y in zip(ici_mesh_shape, dcn_mesh_shape)])
    self.device_attributes = xr.global_device_attributes()
    if 'slice_index' in self.device_attributes[0] and np.prod(
        dcn_mesh_shape) == 1:
      raise ValueError('Provide dcn_mesh_shape to create a mesh for multislice')
    if 'slice_index' not in self.device_attributes[0] and np.prod(
        dcn_mesh_shape) > 1:
      raise ValueError('Invalid dcn_mesh_shape for single slice mesh')
    self.ici_mesh_shape = ici_mesh_shape
    self.dcn_mesh_shape = dcn_mesh_shape
    if np.prod(dcn_mesh_shape) > 1 and 'slice_index' in self.device_attributes[
        0]:  # multislice
      mesh = self._create_hybrid_device_mesh(self.ici_mesh_shape,
                                             self.dcn_mesh_shape)
    else:
      mesh = self._create_device_mesh(self.ici_mesh_shape)
    device_ids = mesh.flatten()
    super().__init__(device_ids, mesh_shape, axis_names)

  def _get_physical_tpu_mesh(self, devices: Sequence[Any]) -> np.ndarray:
    r"""Rearrange TPU devices in a slice into a physical mesh."""
    assert xm.xla_device_hw(xm.xla_device()) == 'TPU'
    device_coords = [self.device_attributes[d]['coords'] for d in devices]
    dims = tuple(d + 1 for d in max(device_coords))
    out = np.empty(dims, dtype=object)
    for coords, d in zip(device_coords, devices):
      out[coords[0], coords[1], coords[2]] = d
    return out

  def _create_device_mesh_for_nd_torus(
      self, physical_mesh: np.ndarray,
      mesh_shape: Sequence[int]) -> Tuple[np.ndarray, List[Tuple[int, ...]]]:
    # Remaining physical axes to be assigned to logical axes.
    assignable_physical_mesh = list(physical_mesh.shape)
    # Map each logical axis to a subset of physical axes.
    assignment: List[Tuple[int, ...]] = [() for _ in mesh_shape]
    # Assign logical axes from highest network intensity to lowest.
    # `mesh_shape` is assumed to ordered by lowest network intensity first, so
    # reverse it first.
    for logical_axis_index, logical_axis_size in reversed(
        list(enumerate(mesh_shape))):
      for num_axes in range(3, 0, -1):
        axes = itertools.combinations(assignable_physical_mesh, num_axes)
        indices = itertools.combinations(
            range(len(assignable_physical_mesh)), num_axes)
        for c_axes, c_indices in zip(axes, indices):
          if np.product(c_axes) == logical_axis_size:
            assignment[logical_axis_index] = c_indices
            # Zero the assigned physical axes.
            assignable_physical_mesh = [
                0 if i in c_indices else v
                for i, v in enumerate(assignable_physical_mesh)
            ]
            break
        if assignment[logical_axis_index]:
          # We already found an assignment from one candidate above.
          break
      else:
        # If the num_axes for loop did not break, i.e. none of the candidates work
        # goto here with this while-else construct.
        if logical_axis_size > 1:
          raise NotImplementedError(
              'Failed to find assignment for logical_axis_index'
              f' {logical_axis_index} of size {logical_axis_size} with remaining'
              f' assignable mesh {assignable_physical_mesh}. The size of each'
              ' axis in your logical mesh must be equal to the product of'
              ' some subset of the physical mesh axis sizes. E.g logical mesh (4,'
              ' 16) is compatible with physical mesh 4x4x4 since 4=4 and 16=4x4.'
          )
    # Flatten the assignment
    transpose: List[int] = []
    for x in assignment:
      for y in x:
        transpose.append(int(y))
    return physical_mesh.transpose(transpose).reshape(mesh_shape), assignment

  def _create_device_mesh(self,
                          mesh_shape: Sequence[int],
                          devices: Sequence[Any] = None) -> np.ndarray:
    if devices is None:
      devices = np.arange(xr.global_device_count())
    if np.prod(mesh_shape) != len(devices):
      raise ValueError(
          f'Number of devices {len(devices)} must equal the product '
          f'of mesh_shape {mesh_shape}')
    physical_mesh = self._get_physical_tpu_mesh(devices)
    device_mesh, assignment = self._create_device_mesh_for_nd_torus(
        physical_mesh, mesh_shape)
    return device_mesh

  def _create_hybrid_device_mesh(self, ici_mesh_shape: Sequence[int],
                                 dcn_mesh_shape: Sequence[int]) -> np.ndarray:
    """Creates a device mesh based on ici and dcn mesh shape.
    """
    granule_dict = defaultdict(list)
    for d, dev in enumerate(self.device_attributes):
      granule_dict[dev['slice_index']].append(d)
    # sorts devices based on slice_index.
    granules = list(granule_dict[key] for key in sorted(granule_dict.keys()))
    if np.prod(dcn_mesh_shape) != len(granules):
      raise ValueError(
          f'Number of slices {len(granules)} must equal the product of '
          f'dcn_mesh_shape {dcn_mesh_shape}')
    # creates a seperate internal mesh for each slice.
    per_granule_meshes = [
        self._create_device_mesh(ici_mesh_shape, granule)
        for granule in granules
    ]
    granule_mesh = np.arange(len(granules)).reshape(dcn_mesh_shape)
    blocks = np.vectorize(
        lambda i: per_granule_meshes[i], otypes=[object])(
            granule_mesh)
    device_mesh = np.block(blocks.tolist())
    return device_mesh


class ShardingType(IntEnum):
  # ShardingType enum ID maps to OpSharidng.Type (https://shorturl.at/pvAJX)
  REPLICATED = 0
  MAXIMAL = 1
  TUPLE = 2
  TILED = 3
  MANUAL = 4
  PARTIAL = 5


def _get_sharding_type(partition_spec: Tuple[Union[int, None]],
                       num_devices: int) -> ShardingType:
  sharding_type = ShardingType.TILED
  if num_devices == 1:
    sharding_type = ShardingType.MAXIMAL
  elif all(d is None for d in partition_spec):
    sharding_type = ShardingType.REPLICATED
  elif any(d is None for d in partition_spec):
    sharding_type = ShardingType.PARTIAL
  return sharding_type


def _get_tile_assignment(mesh: Mesh) -> List[int]:
  return mesh.get_logical_mesh().tolist()


def _get_group_assignment(
    sharding_type: ShardingType, mesh: Mesh,
    partition_spec: Tuple[Union[int, None]]) -> Tuple[List, List]:
  group_assignment = list()
  replication_groups = list()
  if sharding_type is ShardingType.PARTIAL:
    # Shard across groups and replicate within subgroups; replicated dims
    # will be used to group replication devices.
    tile_dims = [d for d in partition_spec if d is not None]
    replicated_dims = set(range(len(mesh.mesh_shape))) - set(tile_dims)

    group_list = [np.array(mesh.get_logical_mesh().tolist())]
    for d in tile_dims:
      _group_list = list()
      for group_members in group_list:
        _group_list += np.split(group_members, mesh.mesh_shape[d], d)
      group_list = _group_list
    replication_groups = [group.flatten().tolist() for group in group_list]

    group_tile_shape = list(mesh.mesh_shape)
    for d in replicated_dims:
      group_tile_shape[d] = 1
    group_assignment = np.arange(len(replication_groups)).reshape(
        tuple(group_tile_shape)).tolist()
  return group_assignment, replication_groups


@xr.requires_pjrt
def mark_sharding(t: Union[torch.Tensor, XLAShardedTensor], mesh: Mesh,
                  partition_spec: Tuple[Union[int, None]]) -> XLAShardedTensor:
  """
    Annotates the tensor provided with XLA partition spec. Internally,
    it annotates the corresponding XLATensor as sharded for the XLA SpmdPartitioner pass.
    Args:
        t (Union[torch.Tensor, XLAShardedTensor]): input tensor to be annotated with partition_spec.

        mesh (Mesh): describes the logical XLA device topology and the underlying device IDs.

        partition_spec (Tuple[int, None]): A tuple of device_mesh dimension index or `None`.
        This specifies how each input rank is sharded (index to mesh_shape) or replicated (None).
        For example, we can shard an 8x10 tensor 4-way row-wise, and replicate column-wise.
        >> input = torch.randn(8, 10)
        >> mesh_shape = (4, 2)
        >> partition_spec = (0, None)

    Examples
    —------------------------------
    mesh_shape = (4, 2)
    num_devices = xr.global_device_count()
    device_ids = np.array(range(num_devices))
    mesh = Mesh(device_ids, mesh_shape, ('x', 'y'))

    # 4-way data parallel
    input = torch.randn(8, 32).to(xm.xla_device())
    xs.mark_sharding(input, mesh, (0, None))

    # 2-way model parallel
    linear = nn.Linear(32, 10).to(xm.xla_device())
    xs.mark_sharding(linear.weight, mesh, (None, 1))
  """
  num_devices = xr.global_device_count()
  assert num_devices > 0, "This requires XLA supported device(s)."
  assert mesh.size() == num_devices, \
    f"{mesh.mesh_shape} is not mappable over {num_devices} devices."
  assert all((d >= 0 and d < len(mesh.mesh_shape)) for d in partition_spec if d), \
    f"partition_spec ({partition_spec}) contains out of bound index into mesh_shape."
  # We only allow fully specified `partition_spec` to be applicable, as opposed
  # to filling in the unspecified replicated dims. Fully specified `partiion_spec`
  # should be of the same rank as `t`. This is to support partial replication
  # where the group assignment may vary with different input ranks.
  assert len(t.shape) == len(partition_spec), \
    f"Partition spec length ({len(partition_spec)}) should be equal to the input rank ({len(t.shape)})."
  specs = [d for d in partition_spec if d]
  assert len(specs) == len(np.unique(specs)), \
    f"Each device mesh dimension should appear at most once in partition_spec {partition_spec}."

  tile_assignment = _get_tile_assignment(mesh)
  sharding_type = _get_sharding_type(partition_spec, num_devices)
  group_assignment, replication_groups = _get_group_assignment(
      sharding_type, mesh, partition_spec)

  if isinstance(t, XLAShardedTensor):
    torch_xla._XLAC._xla_mark_sharding(t.global_tensor, tile_assignment,
                                       group_assignment, replication_groups,
                                       int(sharding_type))
    return t
  torch_xla._XLAC._xla_mark_sharding(t, tile_assignment, group_assignment,
                                     replication_groups, int(sharding_type))
  return XLAShardedTensor(t)


def clear_sharding(t: Union[torch.Tensor, XLAShardedTensor]) -> torch.Tensor:
  """Clear sharding annotation from the input tensor and return a `cpu` casted tensor."""
  torch_xla._XLAC._xla_clear_sharding(t)
  if isinstance(t, XLAShardedTensor):
    return t.global_tensor
  return t


def wrap_if_sharded(x: Any) -> Any:
  """
  If the input is a sharded tensor, return an XLAShardedTensor wrapping it.
  Otherwise, returns the input.
  """
  if (isinstance(x, torch.Tensor) and not isinstance(x, XLAShardedTensor) and
      torch_xla._XLAC._get_xla_sharding_type(x) is not None):
    return XLAShardedTensor(x)
  return x


@dataclass
class ShardingSpec:
  mesh: Mesh
  partition_spec: Tuple[Union[int, None]]

  # Derived fields
  _tile_assignment: List[int] = field(init=False)
  _group_assignment: List[int] = field(init=False)
  _replication_groups: List[int] = field(init=False)
  _sharding_type: ShardingType = field(init=False)

  @xr.requires_pjrt
  def __post_init__(self):
    partition_spec, mesh = self.partition_spec, self.mesh
    self._tile_assignment = _get_tile_assignment(mesh)
    self._sharding_type = _get_sharding_type(partition_spec,
                                             xr.global_device_count())
    self._group_assignment, self._replication_groups = _get_group_assignment(
        self._sharding_type, mesh, partition_spec)

  def xla_spec(self, t: torch.Tensor) -> Union['XlaShardingSpec', None]:
    """
    Create an XlaShardingSpec for the given tensor. If the tensor is
    incompatible with the ShardingSpec, returns None.
    """
    if not self.can_apply(t):
      return None
    return torch_xla._XLAC.XlaShardingSpec(t, self._tile_assignment,
                                           self._group_assignment,
                                           self._replication_groups,
                                           int(self._sharding_type))

  def can_apply(self, t: torch.Tensor) -> bool:
    """
    Test whether the ShardingSpec is compatible with the given torch.Tensor.
    """
    return len(t.shape) == len(self.partition_spec)

  def apply(self, t: torch.Tensor):
    # TODO(yeounoh) use virtual device interface when available.
    assert (t.device == xm.xla_device())
    mark_sharding(t, self.mesh, self.partition_spec)
