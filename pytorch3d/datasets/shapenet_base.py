# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import warnings
from os import path
from typing import Dict, List, Optional

import torch
from pytorch3d.io import load_objs_as_meshes
from pytorch3d.renderer import (
    HardPhongShader,
    MeshRasterizer,
    MeshRenderer,
    OpenGLPerspectiveCameras,
    PointLights,
    RasterizationSettings,
)
from pytorch3d.structures import Textures


class ShapeNetBase(torch.utils.data.Dataset):
    """
    'ShapeNetBase' implements a base Dataset for ShapeNet and R2N2 with helper methods.
    It is not intended to be used on its own as a Dataset for a Dataloader. Both __init__
    and __getitem__ need to be implemented.
    """

    def __init__(self):
        """
        Set up lists of synset_ids and model_ids.
        """
        self.synset_ids = []
        self.model_ids = []
        self.synset_inv = {}
        self.synset_starts = {}
        self.synset_lens = {}
        self.shapenet_dir = ""
        self.model_dir = "model.obj"

    def __len__(self):
        """
        Return number of total models in the loaded dataset.
        """
        return len(self.model_ids)

    def __getitem__(self, idx) -> Dict:
        """
        Read a model by the given index. Need to be implemented for every child class
        of ShapeNetBase.

        Args:
            idx: The idx of the model to be retrieved in the dataset.

        Returns:
            dictionary containing information about the model.
        """
        raise NotImplementedError(
            "__getitem__ should be implemented in the child class of ShapeNetBase"
        )

    def _get_item_ids(self, idx) -> Dict:
        """
        Read a model by the given index.

        Args:
            idx: The idx of the model to be retrieved in the dataset.

        Returns:
            dictionary with following keys:
            - synset_id (str): synset id
            - model_id (str): model id
        """
        model = {}
        model["synset_id"] = self.synset_ids[idx]
        model["model_id"] = self.model_ids[idx]
        return model

    def render(
        self,
        model_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        sample_nums: Optional[List[int]] = None,
        idxs: Optional[List[int]] = None,
        shader_type=HardPhongShader,
        device="cpu",
        **kwargs
    ) -> torch.Tensor:
        """
        If a list of model_ids are supplied, render all the objects by the given model_ids.
        If no model_ids are supplied, but categories and sample_nums are specified, randomly
        select a number of objects (number specified in sample_nums) in the given categories
        and render these objects. If instead a list of idxs is specified, check if the idxs
        are all valid and render models by the given idxs. Otherwise, randomly select a number
        (first number in sample_nums, default is set to be 1) of models from the loaded dataset
        and render these models.

        Args:
            model_ids: List[str] of model_ids of models intended to be rendered.
            categories: List[str] of categories intended to be rendered. categories
                and sample_nums must be specified at the same time. categories can be given
                in the form of synset offsets or labels, or a combination of both.
            sample_nums: List[int] of number of models to be randomly sampled from
                each category. Could also contain one single integer, in which case it
                will be broadcasted for every category.
            idxs: List[int] of indices of models to be rendered in the dataset.
            shader_type: Select shading. Valid options include HardPhongShader (default),
                SoftPhongShader, HardGouraudShader, SoftGouraudShader, HardFlatShader,
                SoftSilhouetteShader.
            device: torch.device on which the tensors should be located.
            **kwargs: Accepts any of the kwargs that the renderer supports.

        Returns:
            Batch of rendered images of shape (N, H, W, 3).
        """
        paths = self._handle_render_inputs(model_ids, categories, sample_nums, idxs)
        meshes = load_objs_as_meshes(paths, device=device, load_textures=False)
        meshes.textures = Textures(
            verts_rgb=torch.ones_like(meshes.verts_padded(), device=device)
        )
        cameras = kwargs.get("cameras", OpenGLPerspectiveCameras()).to(device)
        renderer = MeshRenderer(
            rasterizer=MeshRasterizer(
                cameras=cameras,
                raster_settings=kwargs.get("raster_settings", RasterizationSettings()),
            ),
            shader=shader_type(
                device=device,
                cameras=cameras,
                lights=kwargs.get("lights", PointLights()).to(device),
            ),
        )
        return renderer(meshes)

    def _handle_render_inputs(
        self,
        model_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        sample_nums: Optional[List[int]] = None,
        idxs: Optional[List[int]] = None,
    ) -> List[str]:
        """
        Helper function for converting user provided model_ids, categories and sample_nums
        to indices of models in the loaded dataset. If model idxs are provided, we check if
        the idxs are valid. If no models are specified, the first model in the loaded dataset
        is chosen. The function returns the file paths to the selected models.

        Args:
            model_ids: List[str] of model_ids of models to be rendered.
            categories: List[str] of categories to be rendered.
            sample_nums: List[int] of number of models to be randomly sampled from
                each category.
            idxs: List[int] of indices of models to be rendered in the dataset.

        Returns:
            List of paths of models to be rendered.
        """
        # Get corresponding indices if model_ids are supplied.
        if model_ids is not None and len(model_ids) > 0:
            idxs = []
            for model_id in model_ids:
                if model_id not in self.model_ids:
                    raise ValueError(
                        "model_id %s not found in the loaded dataset." % model_id
                    )
                idxs.append(self.model_ids.index(model_id))

        # Sample random models if categories and sample_nums are supplied and get
        # the corresponding indices.
        elif categories is not None and len(categories) > 0:
            sample_nums = [1] if sample_nums is None else sample_nums
            if len(categories) != len(sample_nums) and len(sample_nums) != 1:
                raise ValueError(
                    "categories and sample_nums needs to be of the same length or "
                    "sample_nums needs to be of length 1."
                )

            idxs_tensor = torch.empty(0, dtype=torch.int32)
            for i in range(len(categories)):
                category = self.synset_inv.get(categories[i], categories[i])
                if category not in self.synset_inv.values():
                    raise ValueError(
                        "Category %s is not in the loaded dataset." % category
                    )
                # Broadcast if sample_nums has length of 1.
                sample_num = sample_nums[i] if len(sample_nums) > 1 else sample_nums[0]
                sampled_idxs = self._sample_idxs_from_category(
                    sample_num=sample_num, category=category
                )
                idxs_tensor = torch.cat((idxs_tensor, sampled_idxs))
            idxs = idxs_tensor.tolist()
        # Check if the indices are valid if idxs are supplied.
        elif idxs is not None and len(idxs) > 0:
            if any(idx < 0 or idx >= len(self.model_ids) for idx in idxs):
                raise IndexError(
                    "One or more idx values are out of bounds. Indices need to be"
                    "between 0 and %s." % (len(self.model_ids) - 1)
                )
        # Check if sample_nums is specified, if so sample sample_nums[0] number
        # of indices from the entire loaded dataset. Otherwise randomly select one
        # index from the dataset.
        else:
            sample_nums = [1] if sample_nums is None else sample_nums
            if len(sample_nums) > 1:
                msg = (
                    "More than one sample sizes specified, now sampling "
                    "%d models from the dataset." % sample_nums[0]
                )
                warnings.warn(msg)
            idxs = self._sample_idxs_from_category(sample_nums[0])
        return [
            path.join(
                self.shapenet_dir,
                self.synset_ids[idx],
                self.model_ids[idx],
                self.model_dir,
            )
            for idx in idxs
        ]

    def _sample_idxs_from_category(
        self, sample_num: int = 1, category: Optional[str] = None
    ) -> List[int]:
        """
        Helper function for sampling a number of indices from the given category.

        Args:
            sample_num: number of indicies to be sampled from the given category.
            category: category synset of the category to be sampled from. If not
                specified, sample from all models in the loaded dataset.
        """
        start = self.synset_starts[category] if category is not None else 0
        range_len = (
            self.synset_lens[category] if category is not None else self.__len__()
        )
        replacement = sample_num > range_len
        sampled_idxs = (
            torch.multinomial(
                torch.ones((range_len), dtype=torch.float32),
                sample_num,
                replacement=replacement,
            )
            + start
        )
        if replacement:
            msg = (
                "Sample size %d is larger than the number of objects in %s, "
                "values sampled with replacement."
            ) % (
                sample_num,
                "category " + category if category is not None else "all categories",
            )
            warnings.warn(msg)
        return sampled_idxs
