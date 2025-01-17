from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from kornia.geometry.transform import center_crop, rotate
from torch import Tensor
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data._utils.collate import default_collate

from trajdata.augmentation import BatchAugmentation
from trajdata.data_structures.batch import AgentBatch, SceneBatch
from trajdata.data_structures.batch_element import AgentBatchElement, SceneBatchElement
from trajdata.utils import arr_utils


def map_collate_fn_agent(
    batch_elems: List[AgentBatchElement],
):
    if batch_elems[0].map_patch is None:
        return None, None, None

    patch_data: Tensor = torch.as_tensor(
        np.stack([batch_elem.map_patch.data for batch_elem in batch_elems]),
        dtype=torch.float,
    )
    rot_angles: Tensor = torch.as_tensor(
        [batch_elem.map_patch.rot_angle for batch_elem in batch_elems],
        dtype=torch.float,
    )
    patch_size: int = batch_elems[0].map_patch.crop_size
    assert all(
        batch_elem.map_patch.crop_size == patch_size for batch_elem in batch_elems
    )

    resolution: Tensor = torch.as_tensor(
        [batch_elem.map_patch.resolution for batch_elem in batch_elems],
        dtype=torch.float,
    )
    rasters_from_world_tf: Tensor = torch.as_tensor(
        np.stack(
            [batch_elem.map_patch.raster_from_world_tf for batch_elem in batch_elems]
        ),
        dtype=torch.float,
    )

    if (
        torch.count_nonzero(rot_angles) == 0
        and patch_size == patch_data.shape[-1] == patch_data.shape[-2]
    ):
        rasters_from_world_tf = torch.bmm(
            torch.tensor(
                [
                    [
                        [1.0, 0.0, patch_size // 2],
                        [0.0, 1.0, patch_size // 2],
                        [0.0, 0.0, 1.0],
                    ]
                ],
                dtype=rasters_from_world_tf.dtype,
                device=rasters_from_world_tf.device,
            ).expand((rasters_from_world_tf.shape[0], -1, -1)),
            rasters_from_world_tf,
        )

        rot_crop_patches = patch_data
    else:

        rot_crop_patches: Tensor = center_crop(
            rotate(patch_data, torch.rad2deg(rot_angles)), (patch_size, patch_size)
        )
        rasters_from_world_tf = torch.bmm(
            arr_utils.transform_matrices(
                -rot_angles,
                torch.tensor([[patch_size // 2, patch_size // 2]]).expand(
                    (rot_angles.shape[0], -1)
                ),
            ),
            rasters_from_world_tf,
        )

    return (
        rot_crop_patches,
        resolution,
        rasters_from_world_tf,
    )


def map_collate_fn_scene(
    batch_elems: List[SceneBatchElement],
    max_agent_num: Optional[int] = None,
    pad_value: Any = np.nan,
) -> Tuple[Optional[Tensor], Optional[Tensor], Optional[Tensor]]:

    if batch_elems[0].map_patches is None:
        return None, None, None

    patch_size: int = batch_elems[0].map_patches[0].crop_size
    assert all(
        batch_elem.map_patches[0].crop_size == patch_size for batch_elem in batch_elems
    )

    num_agents: List[int] = list()
    agents_rasters_from_world_tfs: List[np.ndarray] = list()
    agents_patches: List[np.ndarray] = list()
    agents_rot_angles_list: List[float] = list()
    agents_res_list: List[float] = list()

    for elem in batch_elems:
        num_agents.append(min(elem.num_agents, max_agent_num))
        agents_rasters_from_world_tfs += [
            x.raster_from_world_tf for x in elem.map_patches[:max_agent_num]
        ]
        agents_patches += [x.data for x in elem.map_patches[:max_agent_num]]
        agents_rot_angles_list += [
            x.rot_angle for x in elem.map_patches[:max_agent_num]
        ]
        agents_res_list += [x.resolution for x in elem.map_patches[:max_agent_num]]

    patch_data: Tensor = torch.as_tensor(np.stack(agents_patches), dtype=torch.float)
    agents_rot_angles: Tensor = torch.as_tensor(
        np.stack(agents_rot_angles_list), dtype=torch.float
    )
    agents_rasters_from_world_tf: Tensor = torch.as_tensor(
        np.stack(agents_rasters_from_world_tfs), dtype=torch.float
    )
    agents_resolution: Tensor = torch.as_tensor(
        np.stack(agents_res_list), dtype=torch.int
    )

    if torch.count_nonzero(agents_rot_angles) == 0:
        agents_rasters_from_world_tf = torch.bmm(
            torch.tensor(
                [
                    [
                        [1.0, 0.0, patch_size // 2],
                        [0.0, 1.0, patch_size // 2],
                        [0.0, 0.0, 1.0],
                    ]
                ],
                dtype=agents_rasters_from_world_tf.dtype,
                device=agents_rasters_from_world_tf.device,
            ).expand((agents_rasters_from_world_tf.shape[0], -1, -1)),
            agents_rasters_from_world_tf,
        )

        rot_crop_patches = patch_data
    else:
        agents_rasters_from_world_tf = torch.bmm(
            arr_utils.transform_matrices(
                -agents_rot_angles,
                torch.tensor([[patch_size // 2, patch_size // 2]]).expand(
                    (agents_rot_angles.shape[0], -1)
                ),
            ),
            agents_rasters_from_world_tf,
        )
        rot_crop_patches = center_crop(
            rotate(patch_data, torch.rad2deg(agents_rot_angles)),
            (patch_size, patch_size),
        )

    rot_crop_patches = split_pad_crop(
        rot_crop_patches, num_agents, pad_value=pad_value, desired_size=max_agent_num
    )

    agents_rasters_from_world_tf = split_pad_crop(
        agents_rasters_from_world_tf,
        num_agents,
        pad_value=pad_value,
        desired_size=max_agent_num,
    )
    agents_resolution = split_pad_crop(
        agents_resolution, num_agents, pad_value=0, desired_size=max_agent_num
    )

    return rot_crop_patches, agents_resolution, agents_rasters_from_world_tf


def agent_collate_fn(
    batch_elems: List[AgentBatchElement],
    return_dict: bool,
    batch_augments: Optional[List[BatchAugmentation]] = None,
) -> Union[AgentBatch, Dict[str, Any]]:
    batch_size: int = len(batch_elems)

    data_index_t: Tensor = torch.zeros((batch_size,), dtype=torch.int)
    dt_t: Tensor = torch.zeros((batch_size,), dtype=torch.float)
    agent_type_t: Tensor = torch.zeros((batch_size,), dtype=torch.int)
    agent_names: List[str] = list()

    curr_agent_state: List[Tensor] = list()

    agent_history: List[Tensor] = list()
    agent_history_extent: List[Tensor] = list()
    agent_history_len: Tensor = torch.zeros((batch_size,), dtype=torch.long)

    agent_future: List[Tensor] = list()
    agent_future_extent: List[Tensor] = list()
    agent_future_len: Tensor = torch.zeros((batch_size,), dtype=torch.long)

    num_neighbors_t: Tensor = torch.as_tensor(
        [elem.num_neighbors for elem in batch_elems], dtype=torch.long
    )
    max_num_neighbors: int = num_neighbors_t.max().item()

    neighbor_types: List[Tensor] = list()
    neighbor_histories: List[Tensor] = list()
    neighbor_history_extents: List[Tensor] = list()
    neighbor_futures: List[Tensor] = list()
    neighbor_future_extents: List[Tensor] = list()

    # Doing this one up here so that I can use it later in the loop.
    if max_num_neighbors > 0:
        neighbor_history_lens_t: Tensor = pad_sequence(
            [
                torch.as_tensor(elem.neighbor_history_lens_np, dtype=torch.long)
                for elem in batch_elems
            ],
            batch_first=True,
            padding_value=0,
        )
        max_neigh_history_len: int = neighbor_history_lens_t.max().item()

        neighbor_future_lens_t: Tensor = pad_sequence(
            [
                torch.as_tensor(elem.neighbor_future_lens_np, dtype=torch.long)
                for elem in batch_elems
            ],
            batch_first=True,
            padding_value=0,
        )
        max_neigh_future_len: int = neighbor_future_lens_t.max().item()
    else:
        neighbor_history_lens_t: Tensor = torch.full((batch_size, 0), np.nan)
        max_neigh_history_len: int = 0

        neighbor_future_lens_t: Tensor = torch.full((batch_size, 0), np.nan)
        max_neigh_future_len: int = 0

    robot_future: List[Tensor] = list()
    robot_future_len: Tensor = torch.zeros((batch_size,), dtype=torch.long)

    elem: AgentBatchElement
    for idx, elem in enumerate(batch_elems):
        data_index_t[idx] = elem.data_index
        dt_t[idx] = elem.dt
        agent_names.append(elem.agent_name)
        agent_type_t[idx] = elem.agent_type.value

        curr_agent_state.append(
            torch.as_tensor(elem.curr_agent_state_np, dtype=torch.float)
        )

        agent_history.append(
            torch.as_tensor(elem.agent_history_np, dtype=torch.float).flip(-2)
        )
        agent_history_extent.append(
            torch.as_tensor(elem.agent_history_extent_np, dtype=torch.float).flip(-2)
        )
        agent_history_len[idx] = elem.agent_history_len

        agent_future.append(torch.as_tensor(elem.agent_future_np, dtype=torch.float))
        agent_future_extent.append(
            torch.as_tensor(elem.agent_future_extent_np, dtype=torch.float)
        )
        agent_future_len[idx] = elem.agent_future_len

        neighbor_types.append(
            torch.as_tensor(elem.neighbor_types_np, dtype=torch.float)
        )

        if elem.num_neighbors > 0:
            # History
            padded_neighbor_histories = pad_sequence(
                [
                    torch.as_tensor(nh, dtype=torch.float).flip(-2)
                    for nh in elem.neighbor_histories
                ],
                batch_first=True,
                padding_value=np.nan,
            ).flip(-2)
            padded_neighbor_history_extents = pad_sequence(
                [
                    torch.as_tensor(nh, dtype=torch.float).flip(-2)
                    for nh in elem.neighbor_history_extents
                ],
                batch_first=True,
                padding_value=np.nan,
            ).flip(-2)
            if padded_neighbor_histories.shape[-2] < max_neigh_history_len:
                to_add = max_neigh_history_len - padded_neighbor_histories.shape[-2]
                padded_neighbor_histories = F.pad(
                    padded_neighbor_histories,
                    pad=(0, 0, to_add, 0),
                    mode="constant",
                    value=np.nan,
                )
                padded_neighbor_history_extents = F.pad(
                    padded_neighbor_history_extents,
                    pad=(0, 0, to_add, 0),
                    mode="constant",
                    value=np.nan,
                )

            neighbor_histories.append(
                padded_neighbor_histories.reshape(
                    (-1, padded_neighbor_histories.shape[-1])
                )
            )
            neighbor_history_extents.append(
                padded_neighbor_history_extents.reshape(
                    (-1, padded_neighbor_history_extents.shape[-1])
                )
            )

            # Future
            padded_neighbor_futures = pad_sequence(
                [
                    torch.as_tensor(nh, dtype=torch.float)
                    for nh in elem.neighbor_futures
                ],
                batch_first=True,
                padding_value=np.nan,
            )
            padded_neighbor_future_extents = pad_sequence(
                [
                    torch.as_tensor(nh, dtype=torch.float)
                    for nh in elem.neighbor_future_extents
                ],
                batch_first=True,
                padding_value=np.nan,
            )
            if padded_neighbor_futures.shape[-2] < max_neigh_future_len:
                to_add = max_neigh_future_len - padded_neighbor_futures.shape[-2]
                padded_neighbor_futures = F.pad(
                    padded_neighbor_futures,
                    pad=(0, 0, 0, to_add),
                    mode="constant",
                    value=np.nan,
                )
                padded_neighbor_future_extents = F.pad(
                    padded_neighbor_future_extents,
                    pad=(0, 0, 0, to_add),
                    mode="constant",
                    value=np.nan,
                )

            neighbor_futures.append(
                padded_neighbor_futures.reshape((-1, padded_neighbor_futures.shape[-1]))
            )
            neighbor_future_extents.append(
                padded_neighbor_future_extents.reshape(
                    (-1, padded_neighbor_future_extents.shape[-1])
                )
            )
        else:
            # If there's no neighbors, make the state dimension match the
            # agent history state dimension (presumably they'll be the same
            # since they're obtained from the same cached data source).
            neighbor_histories.append(
                torch.full((0, elem.agent_history_np.shape[-1]), np.nan)
            )
            neighbor_history_extents.append(
                torch.full((0, elem.agent_history_extent_np.shape[-1]), np.nan)
            )

            neighbor_futures.append(
                torch.full((0, elem.agent_future_np.shape[-1]), np.nan)
            )
            neighbor_future_extents.append(
                torch.full((0, elem.agent_future_extent_np.shape[-1]), np.nan)
            )

        if elem.robot_future_np is not None:
            robot_future.append(
                torch.as_tensor(elem.robot_future_np, dtype=torch.float)
            )
            robot_future_len[idx] = elem.robot_future_len

    curr_agent_state_t: Tensor = torch.stack(curr_agent_state)

    agent_history_t: Tensor = pad_sequence(
        agent_history, batch_first=True, padding_value=np.nan
    ).flip(-2)
    agent_history_extent_t: Tensor = pad_sequence(
        agent_history_extent, batch_first=True, padding_value=np.nan
    ).flip(-2)

    agent_future_t: Tensor = pad_sequence(
        agent_future, batch_first=True, padding_value=np.nan
    )
    agent_future_extent_t: Tensor = pad_sequence(
        agent_future_extent, batch_first=True, padding_value=np.nan
    )

    if max_num_neighbors > 0:
        neighbor_types_t: Tensor = pad_sequence(
            neighbor_types, batch_first=True, padding_value=-1
        )

        neighbor_histories_t: Tensor = pad_sequence(
            neighbor_histories, batch_first=True, padding_value=np.nan
        ).reshape(
            (
                batch_size,
                max_num_neighbors,
                max_neigh_history_len,
                agent_history_t.shape[-1],
            )
        )
        neighbor_history_extents_t: Tensor = pad_sequence(
            neighbor_history_extents, batch_first=True, padding_value=np.nan
        ).reshape(
            (
                batch_size,
                max_num_neighbors,
                max_neigh_history_len,
                agent_history_extent_t.shape[-1],
            )
        )

        neighbor_futures_t: Tensor = pad_sequence(
            neighbor_futures, batch_first=True, padding_value=np.nan
        ).reshape(
            (
                batch_size,
                max_num_neighbors,
                max_neigh_future_len,
                agent_future_t.shape[-1],
            )
        )
        neighbor_future_extents_t: Tensor = pad_sequence(
            neighbor_future_extents, batch_first=True, padding_value=np.nan
        ).reshape(
            (
                batch_size,
                max_num_neighbors,
                max_neigh_future_len,
                agent_future_extent_t.shape[-1],
            )
        )
    else:
        neighbor_types_t: Tensor = torch.full((batch_size, 0), np.nan)

        neighbor_histories_t: Tensor = torch.full(
            (batch_size, 0, max_neigh_history_len, agent_history_t.shape[-1]), np.nan
        )
        neighbor_history_extents_t: Tensor = torch.full(
            (batch_size, 0, max_neigh_history_len, agent_history_extent_t.shape[-1]),
            np.nan,
        )

        neighbor_futures_t: Tensor = torch.full(
            (batch_size, 0, max_neigh_future_len, agent_future_t.shape[-1]), np.nan
        )
        neighbor_future_extents_t: Tensor = torch.full(
            (batch_size, 0, max_neigh_future_len, agent_future_extent_t.shape[-1]),
            np.nan,
        )

    robot_future_t: Optional[Tensor] = (
        pad_sequence(robot_future, batch_first=True, padding_value=np.nan)
        if robot_future
        else None
    )

    (
        map_patches,
        maps_resolution,
        rasters_from_world_tf,
    ) = map_collate_fn_agent(batch_elems)

    agents_from_world_tf = torch.as_tensor(
        np.stack([batch_elem.agent_from_world_tf for batch_elem in batch_elems]),
        dtype=torch.float,
    )

    scene_ids = [batch_elem.scene_id for batch_elem in batch_elems]

    extras: Dict[str, Tensor] = {}
    for key in batch_elems[0].extras.keys():
        extras[key] = torch.as_tensor(np.stack([batch_elem.extras[key] for batch_elem in batch_elems]))

    batch = AgentBatch(
        data_idx=data_index_t,
        dt=dt_t,
        agent_name=agent_names,
        agent_type=agent_type_t,
        curr_agent_state=curr_agent_state_t,
        agent_hist=agent_history_t,
        agent_hist_extent=agent_history_extent_t,
        agent_hist_len=agent_history_len,
        agent_fut=agent_future_t,
        agent_fut_extent=agent_future_extent_t,
        agent_fut_len=agent_future_len,
        num_neigh=num_neighbors_t,
        neigh_types=neighbor_types_t,
        neigh_hist=neighbor_histories_t,
        neigh_hist_extents=neighbor_history_extents_t,
        neigh_hist_len=neighbor_history_lens_t,
        neigh_fut=neighbor_futures_t,
        neigh_fut_extents=neighbor_future_extents_t,
        neigh_fut_len=neighbor_future_lens_t,
        robot_fut=robot_future_t,
        robot_fut_len=robot_future_len,
        maps=map_patches,
        maps_resolution=maps_resolution,
        rasters_from_world_tf=rasters_from_world_tf,
        agents_from_world_tf=agents_from_world_tf,
        scene_ids=scene_ids,
        extras=extras,
    )

    if batch_augments:
        for batch_aug in batch_augments:
            batch_aug.apply_agent(batch)

    if return_dict:
        return asdict(batch)
    return batch


def split_pad_crop(
    batch_tensor, sizes, pad_value: float = 0.0, desired_size: Optional[int] = None
) -> Tensor:
    """Split a batched tensor into different sizes and pad them to the same size

    Args:
        batch_tensor: tensor in bach or split tensor list
        sizes (torch.Tensor): sizes of each entry
        pad_value (float, optional): padding value. Defaults to 0.0
        desired_size (int, optional): desired size. Defaults to None.
    """

    if isinstance(batch_tensor, Tensor):
        x = torch.split(batch_tensor, sizes)
        cat_fun = torch.cat
        full_fun = torch.full
    elif isinstance(batch_tensor, np.ndarray):
        x = np.split(batch_tensor, sizes)
        cat_fun = np.concatenate
        full_fun = np.full
    elif isinstance(batch_tensor, List):
        # already splitted in list
        x = batch_tensor
        if isinstance(batch_tensor[0], Tensor):
            cat_fun = torch.cat
            full_fun = torch.full
        elif isinstance(batch_tensor[0], np.ndarray):
            cat_fun = np.concatenate
            full_fun = np.full
    else:
        raise ValueError("wrong data type for batch tensor")

    x: Tensor = pad_sequence(x, batch_first=True, padding_value=pad_value)
    if desired_size is not None:
        if x.shape[1] >= desired_size:
            x = x[:, :desired_size]
        else:
            bs, max_size = x.shape[:2]
            x = cat_fun(
                (x, full_fun([bs, desired_size - max_size, *x.shape[2:]], pad_value)), 1
            )

    return x


def scene_collate_fn(
    batch_elems: List[SceneBatchElement],
    return_dict: bool,
    batch_augments: Optional[List[BatchAugmentation]] = None,
) -> SceneBatch:
    batch_size: int = len(batch_elems)
    data_index_t: Tensor = torch.zeros((batch_size,), dtype=torch.int)
    dt_t: Tensor = torch.zeros((batch_size,), dtype=torch.float)

    max_agent_num: int = max(elem.num_agents for elem in batch_elems)

    centered_agent_state: List[Tensor] = list()
    agents_types: List[Tensor] = list()
    agents_histories: List[Tensor] = list()
    agents_history_extents: List[Tensor] = list()
    agents_history_len: Tensor = torch.zeros(
        (batch_size, max_agent_num), dtype=torch.long
    )

    agents_futures: List[Tensor] = list()
    agents_future_extents: List[Tensor] = list()
    agents_future_len: Tensor = torch.zeros(
        (batch_size, max_agent_num), dtype=torch.long
    )

    num_agents: List[int] = [elem.num_agents for elem in batch_elems]
    num_agents_t: Tensor = torch.as_tensor(num_agents, dtype=torch.long)

    max_history_len: int = max(elem.agent_history_lens_np.max() for elem in batch_elems)
    max_future_len: int = max(elem.agent_future_lens_np.max() for elem in batch_elems)

    robot_future: List[Tensor] = list()
    robot_future_len: Tensor = torch.zeros((batch_size,), dtype=torch.long)

    for idx, elem in enumerate(batch_elems):
        data_index_t[idx] = elem.data_index
        dt_t[idx] = elem.dt
        centered_agent_state.append(elem.centered_agent_state_np)
        agents_types.append(elem.agent_types_np)
        history_len_i = torch.tensor(
            [rec.shape[0] for rec in elem.agent_histories[:max_agent_num]]
        )
        future_len_i = torch.tensor(
            [rec.shape[0] for rec in elem.agent_futures[:max_agent_num]]
        )
        agents_history_len[idx, : elem.num_agents] = history_len_i
        agents_future_len[idx, : elem.num_agents] = future_len_i

        # History
        padded_agents_histories = pad_sequence(
            [
                torch.as_tensor(nh, dtype=torch.float).flip(-2)
                for nh in elem.agent_histories[:max_agent_num]
            ],
            batch_first=True,
            padding_value=np.nan,
        ).flip(-2)
        padded_agents_history_extents = pad_sequence(
            [
                torch.as_tensor(nh, dtype=torch.float).flip(-2)
                for nh in elem.agent_history_extents[:max_agent_num]
            ],
            batch_first=True,
            padding_value=np.nan,
        ).flip(-2)
        if padded_agents_histories.shape[-2] < max_history_len:
            to_add = max_history_len - padded_agents_histories.shape[-2]
            padded_agents_histories = F.pad(
                padded_agents_histories,
                pad=(0, 0, to_add, 0),
                mode="constant",
                value=np.nan,
            )
            padded_agents_history_extents = F.pad(
                padded_agents_history_extents,
                pad=(0, 0, to_add, 0),
                mode="constant",
                value=np.nan,
            )

        agents_histories.append(padded_agents_histories)
        agents_history_extents.append(padded_agents_history_extents)

        # Future
        padded_agents_futures = pad_sequence(
            [
                torch.as_tensor(nh, dtype=torch.float)
                for nh in elem.agent_futures[:max_agent_num]
            ],
            batch_first=True,
            padding_value=np.nan,
        )
        padded_agents_future_extents = pad_sequence(
            [
                torch.as_tensor(nh, dtype=torch.float)
                for nh in elem.agent_future_extents
            ],
            batch_first=True,
            padding_value=np.nan,
        )
        if padded_agents_futures.shape[-2] < max_future_len:
            to_add = max_future_len - padded_agents_futures.shape[-2]
            padded_agents_futures = F.pad(
                padded_agents_futures,
                pad=(0, 0, 0, to_add),
                mode="constant",
                value=np.nan,
            )
            padded_agents_future_extents = F.pad(
                padded_agents_future_extents,
                pad=(0, 0, 0, to_add),
                mode="constant",
                value=np.nan,
            )

        agents_futures.append(padded_agents_futures)
        agents_future_extents.append(padded_agents_future_extents)

        if elem.robot_future_np is not None:
            robot_future.append(
                torch.as_tensor(elem.robot_future_np, dtype=torch.float)
            )
            robot_future_len[idx] = elem.robot_future_len

    agents_histories_t = split_pad_crop(
        agents_histories, num_agents, np.nan, max_agent_num
    )
    agents_history_extents_t = split_pad_crop(
        agents_history_extents, num_agents, np.nan, max_agent_num
    )
    agents_futures_t = split_pad_crop(agents_futures, num_agents, np.nan, max_agent_num)
    agents_future_extents_t = split_pad_crop(
        agents_future_extents, num_agents, np.nan, max_agent_num
    )

    centered_agent_state_t = torch.tensor(np.stack(centered_agent_state))
    agents_types_t = torch.as_tensor(np.concatenate(agents_types))
    agents_types_t = split_pad_crop(
        agents_types_t, num_agents, pad_value=-1, desired_size=max_agent_num
    )

    map_patches, maps_resolution, rasters_from_world_tf = map_collate_fn_scene(
        batch_elems, max_agent_num
    )
    centered_agent_from_world_tf = torch.as_tensor(
        np.stack(
            [batch_elem.centered_agent_from_world_tf for batch_elem in batch_elems]
        ),
        dtype=torch.float,
    )
    centered_world_from_agent_tf = torch.as_tensor(
        np.stack(
            [batch_elem.centered_world_from_agent_tf for batch_elem in batch_elems]
        ),
        dtype=torch.float,
    )

    robot_future_t: Optional[Tensor] = (
        pad_sequence(robot_future, batch_first=True, padding_value=np.nan)
        if robot_future
        else None
    )

    extras: Dict[str, Tensor] = {}
    for key in batch_elems[0].extras.keys():
        extras[key] = torch.as_tensor(np.stack([batch_elem.extras[key] for batch_elem in batch_elems]))

    batch = SceneBatch(
        data_idx=data_index_t,
        dt=dt_t,
        num_agents=num_agents_t,
        agent_type=agents_types_t,
        centered_agent_state=centered_agent_state_t,
        agent_hist=agents_histories_t,
        agent_hist_extent=agents_history_extents_t,
        agent_hist_len=agents_history_len,
        agent_fut=agents_futures_t,
        agent_fut_extent=agents_future_extents_t,
        agent_fut_len=agents_future_len,
        robot_fut=robot_future_t,
        robot_fut_len=robot_future_len,
        maps=map_patches,
        maps_resolution=maps_resolution,
        rasters_from_world_tf=rasters_from_world_tf,
        centered_agent_from_world_tf=centered_agent_from_world_tf,
        centered_world_from_agent_tf=centered_world_from_agent_tf,
        extras=extras,
    )

    if batch_augments:
        for batch_aug in batch_augments:
            batch_aug.apply_scene(batch)

    if return_dict:
        return asdict(batch)

    return batch
