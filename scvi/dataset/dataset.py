from abc import abstractmethod, ABC
from collections import OrderedDict, defaultdict
from urllib.error import HTTPError
import copy
from dataclasses import dataclass
from functools import partial
import logging
import os
import sys
import urllib.request
from typing import Dict, Iterable, List, Tuple, Union, Optional, Callable

import numpy as np
import pandas as pd
import scipy.sparse as sp_sparse
import anndata
import torch
from sklearn.preprocessing import StandardScaler
from torch.utils.data import Dataset
import scanpy as sc

logger = logging.getLogger(__name__)


@dataclass
class CellMeasurement:
    name: str  # Name of the attribute Eg: 'X'
    data: Union[np.ndarray, sp_sparse.csr_matrix]  # Data itself: Eg: X
    columns_attr_name: str  # Name of the column names attribute : Eg: 'gene_names'
    columns: Union[np.ndarray, List[str]]  # Column names: Eg: gene_names


class GeneExpressionDataset(Dataset):
    """Generic class representing RNA counts and annotation information.

    This class is scVI's base dataset class. It gives access to several
    standard attributes: counts, number of cells, number of genes, etc.
    More importantly, it implements gene-based and cell-based filtering methods.
    It also allows the storage of cell and gene annotation information,
    as well as mappings from these annotation attributes to unique identifiers.
    In order to propagate the filtering behaviour correctly through the relevant
    attributes, they are kept in registries (cell, gene, mappings) which are
    iterated through upon any filtering operation.

    Note that the constructor merely instantiates the GeneExpressionDataset objects.
    It should be used in combination with one of the populating method.
    Either:

        * ``populate_from_data``: to populate using a (nb_cells, nb_genes) matrix.
        * ``populate_from_per_batch_array``: to populate using a (n_batches, nb_cells, nb_genes) matrix.
        * ``populate_from_per_batch_list``: to populate using a ``n_batches``-long
            ``list`` of (nb_cells, nb_genes) matrices.
        * ``populate_from_datasets``: to populate using multiple ``GeneExperessionDataset`` objects,
            merged using the intersection of a gene-wise attribute (``gene_names`` by default).

    """

    #############################
    #                           #
    #       CONSTRUCTORS        #
    #                           #
    #############################

    def __init__(self):
        # registers
        self.dataset_versions = set()
        self.gene_attribute_names = set()
        self.cell_attribute_names = set()
        self.cell_categorical_attribute_names = set()
        self.attribute_mappings = defaultdict(list)
        self.cell_measurements_col_mappings = dict()

        # initialize attributes
        self._X = None
        self._batch_indices = None
        self._labels = None
        self.n_batches = None
        self.n_labels = None
        self.gene_names = None
        self.cell_types = None
        self.local_means = None
        self.local_vars = None
        self._norm_X = None
        self._corrupted_X = None

        # attributes that should not be set by initialization methods
        self.protected_attributes = ["X", "_X"]

    def __repr__(self) -> str:
        if self.X is None:
            descr = "GeneExpressionDataset object (unpopulated)"
        else:
            descr = "GeneExpressionDataset object with n_cells x nb_genes = {} x {}".format(
                self.nb_cells, self.nb_genes
            )
            attrs = [
                "dataset_versions",
                "gene_attribute_names",
                "cell_attribute_names",
                "cell_categorical_attribute_names",
                "cell_measurements_col_mappings",
            ]
            for attr_name in attrs:
                attr = getattr(self, attr_name)
                if len(attr) == 0:
                    continue
                if type(attr) is set:
                    descr += "\n    {}: {}".format(attr_name, str(list(attr))[1:-1])
                else:
                    descr += "\n    {}: {}".format(attr_name, str(attr))

        return descr

    def populate_from_data(
        self,
        X: Union[np.ndarray, sp_sparse.csr_matrix],
        Ys: List[CellMeasurement] = None,
        batch_indices: Union[List[int], np.ndarray, sp_sparse.csr_matrix] = None,
        labels: Union[List[int], np.ndarray, sp_sparse.csr_matrix] = None,
        gene_names: Union[List[str], np.ndarray] = None,
        cell_types: Union[List[str], np.ndarray] = None,
        cell_attributes_dict: Dict[str, Union[List, np.ndarray]] = None,
        gene_attributes_dict: Dict[str, Union[List, np.ndarray]] = None,
        remap_attributes: bool = True,
    ):
        """Populates the data attributes of a GeneExpressionDataset object from a (nb_cells, nb_genes) matrix.

        Parameters
        ----------
        X
            RNA counts matrix, sparse format supported (e.g ``scipy.sparse.csr_matrix``).
        Ys
            List of paired count measurements (e.g CITE-seq protein measurements, spatial coordinates)
        batch_indices
            np.ndarray`` with shape (nb_cells,). Maps each cell to the batch
            it originates from. Note that a batch most likely refers to a specific piece
            of tissue or a specific experimental protocol.
        labels
            np.ndarray`` with shape (nb_cells,). Cell-wise labels. Can be mapped
            to cell types using attribute mappings.
        gene_names
            List`` or ``np.ndarray`` with length/shape (nb_genes,).
            Maps each gene to its name.
        cell_types
            Maps each integer label in ``labels`` to a cell type.
        cell_attributes_dict
            List`` or ``np.ndarray`` with shape (nb_cells,).
        gene_attributes_dict
            List`` or ``np.ndarray`` with shape (nb_genes,).
        remap_attributes
            If set to True (default), the function calls
            `remap_categorical_attributes` at the end

        """
        # set the data hidden attribute
        self._X = (
            np.ascontiguousarray(X, dtype=np.float32)
            if isinstance(X, np.ndarray)
            else X
        )

        self.initialize_cell_attribute(
            "batch_indices",
            np.asarray(batch_indices).reshape((-1, 1))
            if batch_indices is not None
            else np.zeros((X.shape[0], 1)),
            categorical=True,
        )
        self.initialize_cell_attribute(
            "labels",
            np.asarray(labels).reshape((-1, 1))
            if labels is not None
            else np.zeros((X.shape[0], 1)),
            categorical=True,
        )

        self.compute_library_size_batch()

        if gene_names is not None:
            gn = np.asarray(gene_names, dtype="<U64")
            self.initialize_gene_attribute("gene_names", gn)
            if len(np.unique(self.gene_names)) != len(self.gene_names):
                logger.warning("Gene names are not unique.")
        if cell_types is not None:
            self.initialize_mapped_attribute(
                "labels", "cell_types", np.asarray(cell_types, dtype="<U128")
            )
        # add dummy cell type, for consistency with labels
        elif labels is None:
            self.initialize_mapped_attribute(
                "labels", "cell_types", np.asarray(["undefined"], dtype="<U128")
            )

        # handle additional attributes
        if cell_attributes_dict:
            for attribute_name, attribute_value in cell_attributes_dict.items():
                self.initialize_cell_attribute(attribute_name, attribute_value)
        if Ys is not None:
            for measurement in Ys:
                self.initialize_cell_measurement(measurement)
        if gene_attributes_dict:
            for attribute_name, attribute_value in gene_attributes_dict.items():
                self.initialize_gene_attribute(attribute_name, attribute_value)

        if remap_attributes:
            self.remap_categorical_attributes()

    def populate_from_per_batch_list(
        self,
        Xs: List[Union[sp_sparse.csr_matrix, np.ndarray]],
        labels_per_batch: Union[np.ndarray, List[np.ndarray]] = None,
        gene_names: Union[List[str], np.ndarray] = None,
        cell_types: Union[List[str], np.ndarray] = None,
        remap_attributes: bool = True,
    ):
        """Populates the data attributes of a GeneExpressionDataset object from a ``n_batches``-long
            ``list`` of (nb_cells, nb_genes) matrices.

        Parameters
        ----------
        Xs
            RNA counts in the form of a list of np.ndarray with shape (..., nb_genes)
        labels_per_batch
            list of cell-wise labels for each batch.
        gene_names
            gene names, stored as ``str``.
        cell_types
            cell types, stored as ``str``.
        remap_attributes
            If set to True (default), the function calls
            `remap_categorical_attributes` at the end

        """
        nb_genes = Xs[0].shape[1]
        if not all(X.shape[1] == nb_genes for X in Xs):
            raise ValueError("All batches must have same nb_genes")

        X = np.concatenate(Xs) if type(Xs[0]) is np.ndarray else sp_sparse.vstack(Xs)
        batch_indices = np.concatenate(
            [i * np.ones(batch.shape[0], dtype=np.int64) for i, batch in enumerate(Xs)]
        )
        labels = (
            np.concatenate(labels_per_batch).astype(np.int64)
            if labels_per_batch is not None
            else None
        )

        self.populate_from_data(
            X=X,
            batch_indices=batch_indices,
            labels=labels,
            gene_names=gene_names,
            cell_types=cell_types,
            remap_attributes=remap_attributes,
        )

    def populate_from_per_label_list(
        self,
        Xs: List[Union[sp_sparse.csr_matrix, np.ndarray]],
        batch_indices_per_label: List[Union[List[int], np.ndarray]] = None,
        gene_names: Union[List[str], np.ndarray] = None,
        remap_attributes: bool = True,
    ):
        """Populates the data attributes of a GeneExpressionDataset object from a ``n_labels``-long
            ``list`` of (nb_cells, nb_genes) matrices.

        Parameters
        ----------
        Xs
            RNA counts in the form of a list of np.ndarray with shape (..., nb_genes)
        batch_indices_per_label
            cell-wise batch indices, for each cell label.
        gene_names
            gene names, stored as ``str``.
        remap_attributes
            If set to True (default), the function calls
            `remap_categorical_attributes` at the end

        """
        nb_genes = Xs[0].shape[1]
        if not all(X.shape[1] == nb_genes for X in Xs):
            raise ValueError("All batches must have same nb_genes")

        X = np.concatenate(Xs) if type(Xs[0]) is np.ndarray else sp_sparse.vstack(Xs)
        labels = np.concatenate(
            [
                i * np.ones(cluster.shape[0], dtype=np.int64)
                for i, cluster in enumerate(Xs)
            ]
        )
        batch_indices = (
            np.concatenate(batch_indices_per_label).astype(np.int64)
            if batch_indices_per_label
            else None
        )

        self.populate_from_data(
            X=X,
            batch_indices=batch_indices,
            labels=labels,
            gene_names=gene_names,
            remap_attributes=remap_attributes,
        )

    def populate_from_datasets(
        self,
        gene_datasets_list: List["GeneExpressionDataset"],
        shared_labels=True,
        mapping_reference_for_sharing: Dict[str, Union[str, None]] = None,
        cell_measurement_intersection: Dict[str, bool] = None,
    ):
        """Populates the data attribute of a GeneExpressionDataset
        from multiple ``GeneExpressionDataset`` objects, merged using the intersection
        of a gene-wise attribute (``gene_names`` by default).

        Warning: The merging procedure modifies the gene_dataset given as inputs

        For gene-wise attributes, only the attributes of the first dataset are kept.
        For cell-wise attributes, either we "concatenate" or add an "offset" corresponding
        to the number of already existing categories.

        Parameters
        ----------
        gene_datasets_list
            GeneExpressionDataset`` objects to be merged.
        shared_labels
            whether to share labels through ``cell_types`` mapping or not. (Default value = True)
        mapping_reference_for_sharing
            Instructions on how to share cell-wise attributes between datasets.
            Keys are the attribute name and values are registered mapped attribute.
            If provided the mapping is merged across all datasets and then the attribute is
            remapped using index backtracking between the old and merged mapping.
            If no mapping is provided, concatenate the values and add an offset
            if the attribute is registered as categorical in the first dataset.
        cell_measurement_intersection
            A dictionary with keys being cell measurement attributes and values being
            True or False. If True, that cell measurement attribute will be intersected across datasets. If False, the
            union is taken. Defaults to intersection for each cell_measurement

        """
        logger.info("Merging datasets. Input objects are modified in place.")
        logger.info(
            "Gene names and cell measurement names are assumed to have a non-null intersection between datasets."
        )

        # set default sharing behaviour for batch_indices and labels
        if mapping_reference_for_sharing is None:
            mapping_reference_for_sharing = {}
        if shared_labels:
            mapping_reference_for_sharing.update({"labels": "cell_types"})
        if cell_measurement_intersection is None:
            cell_measurement_intersection = {}

        # get intersection based on gene_names and keep cell attributes
        # which are present in all datasets
        genes_to_keep = set.intersection(
            *[set(gene_dataset.gene_names) for gene_dataset in gene_datasets_list]
        )
        cell_attributes_to_keep = set.intersection(
            *[
                set(gene_dataset.cell_attribute_names)
                for gene_dataset in gene_datasets_list
            ]
        ) - set(["local_means", "local_vars"])

        # keep gene order
        genes_to_keep = [
            gene for gene in gene_datasets_list[0].gene_names if gene in genes_to_keep
        ]
        logger.info("Keeping {nb_genes} genes".format(nb_genes=len(genes_to_keep)))

        # filter genes
        Xs = []
        for dataset in gene_datasets_list:
            dataset.reorder_genes(first_genes=genes_to_keep, drop_omitted_genes=True)
            dataset.remap_categorical_attributes()
            Xs.append(dataset.X)

        # concatenate data
        if all([type(X) is np.ndarray for X in Xs]):
            X = np.concatenate(Xs)
        # if sparse, cast all to sparse and stack
        else:
            X = sp_sparse.vstack(
                [
                    X
                    if isinstance(X, sp_sparse.csr_matrix)
                    else sp_sparse.csr_matrix(X)
                    for X in Xs
                ]
            )

        self.populate_from_data(X=X)

        # keep all the gene attributes from all the datasets,
        # and keep the version that comes first (in the order given by the dataset list)
        # and keep all mappings (e.g gene types)
        for gene_dataset in gene_datasets_list[::-1]:
            for attribute_name in gene_dataset.gene_attribute_names:
                self.initialize_gene_attribute(
                    attribute_name, getattr(gene_dataset, attribute_name)
                )
                mapping_names = gene_dataset.attribute_mappings[attribute_name]
                for mapping_name in mapping_names:
                    self.initialize_mapped_attribute(
                        attribute_name,
                        mapping_name,
                        getattr(gene_dataset, mapping_name),
                    )

        # handle cell attributes
        for attribute_name in cell_attributes_to_keep:
            ref_mapping_name = mapping_reference_for_sharing.get(attribute_name, None)

            mapping_names_to_keep = list(
                set.intersection(
                    *[
                        set(gene_dataset.attribute_mappings[attribute_name])
                        for gene_dataset in gene_datasets_list
                    ]
                )
            )

            attribute_values = []
            is_categorical = (
                attribute_name in gene_datasets_list[0].cell_categorical_attribute_names
            )
            is_measurement = (
                attribute_name in gene_datasets_list[0].cell_measurements_col_mappings
            )
            if is_categorical:
                logger.debug(attribute_name + " was detected as categorical")
            else:
                logger.debug(
                    attribute_name
                    + " was detected as non categorical because it is non categorical in at least one dataset"
                )

            if is_measurement:
                logger.debug(attribute_name + " was detected as measurement")
            else:
                logger.debug(
                    attribute_name
                    + " was detected as non measurement because it is non measurement in at least one dataset"
                )

            if is_categorical:
                mappings = defaultdict(list)
                if ref_mapping_name is not None:
                    # Share attributes: concatenate shared mapping
                    # then backtrack index to remap categorical attributes
                    if not all(
                        [
                            ref_mapping_name
                            in gene_dataset.attribute_mappings[attribute_name]
                            for gene_dataset in gene_datasets_list
                        ]
                    ):
                        raise ValueError(
                            "Reference mapping {ref_map} for {attr_name} merging"
                            " is not registered in all datasets.".format(
                                ref_map=ref_mapping_name, attr_name=attribute_name
                            )
                        )
                    mappings = defaultdict(OrderedDict)
                    # combine into new mappings, OrderedDict for deterministic result, fifo
                    for mapping_name in mapping_names_to_keep:
                        for gene_dataset in gene_datasets_list:
                            mappings[mapping_name].update(
                                [(s, None) for s in getattr(gene_dataset, mapping_name)]
                            )
                    mappings = {k: list(v) for k, v in mappings.items()}

                    for gene_dataset in gene_datasets_list:
                        local_attribute_values = getattr(gene_dataset, attribute_name)
                        # remap attribute according to old and new mapping
                        old_mapping = list(getattr(gene_dataset, ref_mapping_name))
                        new_categories = [
                            mappings[ref_mapping_name].index(v) for v in old_mapping
                        ]
                        old_categories = list(range(len(old_mapping)))
                        local_attribute_values, _ = remap_categories(
                            local_attribute_values,
                            mapping_from=old_categories,
                            mapping_to=new_categories,
                        )
                        attribute_values.append(local_attribute_values)

                else:
                    # Don't share so concatenate with offset
                    offset = 0
                    for i, gene_dataset in enumerate(gene_datasets_list):
                        local_attribute_values = getattr(gene_dataset, attribute_name)
                        new_values = offset + local_attribute_values
                        attribute_values.append(new_values)
                        offset += np.max(local_attribute_values) + 1
                        for mapping_name in mapping_names_to_keep:
                            mappings[mapping_name].extend(
                                getattr(gene_dataset, mapping_name)
                            )

                self.initialize_cell_attribute(
                    attribute_name, concatenate_arrays(attribute_values)
                )
                for mapping_name, mapping_values in mappings.items():
                    self.initialize_mapped_attribute(
                        attribute_name, mapping_name, mapping_values
                    )

            elif is_measurement:
                columns_attr_name = gene_datasets_list[
                    0
                ].cell_measurements_col_mappings[attribute_name]
                intersect = cell_measurement_intersection.get(attribute_name, True)
                # Intersect or union columns across datasets
                # Individual datasets with missing columns will have 0s for these features
                if intersect is True:
                    columns_to_keep = set.intersection(
                        *[
                            set(getattr(gene_dataset, columns_attr_name))
                            for gene_dataset in gene_datasets_list
                        ]
                    )
                else:
                    columns_to_keep = set.union(
                        *[
                            set(getattr(gene_dataset, columns_attr_name))
                            for gene_dataset in gene_datasets_list
                        ]
                    )
                    logger.info(
                        "Taking the union for {attr}. Missing data will"
                        " be replaced with 0. Use with care.".format(
                            attr=attribute_name
                        )
                    )
                columns_to_keep = np.asarray(list(sorted(columns_to_keep)))
                logger.info(
                    "Keeping {n_cols} columns in {attr}".format(
                        n_cols=len(columns_to_keep), attr=attribute_name
                    )
                )

                for gene_dataset in gene_datasets_list:
                    # Creates a template with new number of features with order defined
                    # in columns_to_keep. Fills data in from source datasets accordingly
                    # Source datasets are modified in place.
                    template = np.zeros(
                        (
                            getattr(gene_dataset, attribute_name).shape[0],
                            len(columns_to_keep),
                        ),
                        dtype=getattr(gene_dataset, attribute_name).dtype,
                    )
                    if type(getattr(gene_dataset, columns_attr_name)) is not np.ndarray:
                        template = sp_sparse.lil_matrix(template)

                    _, ind1, ind2 = np.intersect1d(
                        getattr(gene_dataset, columns_attr_name),
                        columns_to_keep,
                        return_indices=True,
                    )
                    template[:, ind2] = getattr(gene_dataset, attribute_name)[:, ind1]
                    if type(template) is not np.ndarray:
                        template = template.tocsr()
                    setattr(gene_dataset, attribute_name, template)
                    # in-place modified dataset gets column names updated to be accurate
                    setattr(gene_dataset, columns_attr_name, columns_to_keep)

                for i, gene_dataset in enumerate(gene_datasets_list):
                    attribute_values.append(getattr(gene_dataset, attribute_name))

                self.initialize_cell_measurement(
                    CellMeasurement(
                        name=attribute_name,
                        data=concatenate_arrays(attribute_values),
                        columns_attr_name=columns_attr_name,
                        columns=columns_to_keep,
                    )
                )
            else:
                # Simply concatenate
                for i, gene_dataset in enumerate(gene_datasets_list):
                    attribute_values.append(getattr(gene_dataset, attribute_name))

                self.initialize_cell_attribute(
                    attribute_name, concatenate_arrays(attribute_values)
                )

        self.compute_library_size_batch()

    #############################
    #                           #
    #       OVERRIDES,          #
    #       PROPERTIES          #
    #       and REGISTRIES      #
    #                           #
    #############################

    def __len__(self):
        return self.nb_cells

    def __getitem__(self, idx):
        """Implements @abstractcmethod in ``torch.utils.data.dataset.Dataset`` ."""
        return idx

    @property
    def X(self):
        return self._X

    @X.setter
    def X(self, X: Union[np.ndarray, sp_sparse.csr_matrix]):
        """Sets the data attribute ``X`` and (re)computes the library size.

        Parameters
        ----------
        X: data


        """
        n_dim = len(X.shape)
        if n_dim != 2:
            raise ValueError(
                "Gene expression data should be 2-dimensional not {}-dimensional.".format(
                    n_dim
                )
            )
        valid_obs = check_nonnegative_integers(X)
        if valid_obs is False:
            logger.warning(
                "X contains continuous and/or negative values. Please use raw UMI/read counts"
                " with scVI"
            )
        self._X = X
        logger.info("Computing the library size for the new data")
        self.compute_library_size_batch()

    @property
    def nb_cells(self) -> int:
        return self.X.shape[0]

    @property
    def nb_genes(self) -> int:
        return self.X.shape[1]

    @property
    def batch_indices(self) -> np.ndarray:
        return self._batch_indices

    @batch_indices.setter
    def batch_indices(self, batch_indices: Union[List[int], np.ndarray]):
        """Sets batch indices and the number of batches.
        """
        batch_indices = np.asarray(batch_indices, dtype=np.uint16).reshape(-1, 1)
        self.n_batches = len(np.unique(batch_indices))
        self._batch_indices = batch_indices

    @property
    def labels(self) -> np.ndarray:
        return self._labels

    @labels.setter
    def labels(self, labels: Union[List[int], np.ndarray]):
        """Sets labels and the number of labels
        """
        labels = np.asarray(labels, dtype=np.uint16).reshape(-1, 1)
        self.n_labels = len(np.unique(labels))
        self._labels = labels

    @property
    def norm_X(self) -> Union[sp_sparse.csr_matrix, np.ndarray]:
        """Returns a normalized version of X."""
        return self._norm_X

    @norm_X.setter
    def norm_X(self, norm_X: Union[sp_sparse.csr_matrix, np.ndarray]):
        self._norm_X = norm_X
        self.register_dataset_version("norm_X")

    @property
    def corrupted_X(self) -> Union[sp_sparse.csr_matrix, np.ndarray]:
        """Returns the corrupted version of X."""
        return self._corrupted_X

    @corrupted_X.setter
    def corrupted_X(self, corrupted_X: Union[sp_sparse.csr_matrix, np.ndarray]):
        self._corrupted_X = corrupted_X
        self.register_dataset_version("corrupted_X")

    def remap_categorical_attributes(
        self, attributes_to_remap: Optional[List[str]] = None
    ):
        if attributes_to_remap is None:
            attributes_to_remap = self.cell_categorical_attribute_names

        for attribute_name in attributes_to_remap:
            logger.info("Remapping %s to [0,N]" % attribute_name)
            attr = getattr(self, attribute_name)
            mappings_dict = {
                name: getattr(self, name)
                for name in self.attribute_mappings[attribute_name]
            }
            new_attr, _, new_mappings_dict = remap_categories(
                attr, mappings_dict=mappings_dict
            )
            setattr(self, attribute_name, new_attr)
            for name, mapping in new_mappings_dict.items():
                setattr(self, name, mapping)

    def register_dataset_version(self, version_name):
        """Registers a version of the dataset, e.g normalized version.
        """
        self.dataset_versions.add(version_name)

    def initialize_cell_attribute(self, attribute_name, attribute, categorical=False):
        """Sets and registers a cell-wise attribute, e.g annotation information.
        """
        if (attribute_name in self.protected_attributes) or (
            attribute_name in self.gene_attribute_names
        ):
            valid_attribute_name = attribute_name + "_cell"
            logger.warning(
                "{} is a protected attribute or already exists as a gene attribute and cannot be set with this name "
                "in initialize_cell_attribute, changing name to {} and setting".format(
                    attribute_name, valid_attribute_name
                )
            )
            attribute_name = valid_attribute_name
        try:
            len_attribute = attribute.shape[0]
        except AttributeError:
            len_attribute = len(attribute)
        if not self.nb_cells == len_attribute:
            raise ValueError(
                "Number of cells ({n_cells}) and length of cell attribute ({n_attr}) mismatch".format(
                    n_cells=self.nb_cells, n_attr=len_attribute
                )
            )
        setattr(
            self,
            attribute_name,
            np.asarray(attribute)
            if not isinstance(attribute, sp_sparse.csr_matrix)
            else attribute,
        )
        self.cell_attribute_names.add(attribute_name)
        if categorical:
            self.cell_categorical_attribute_names.add(attribute_name)

    def initialize_cell_measurement(self, measurement: CellMeasurement):
        """Initializes a cell measurement: set attributes and update registers
        """
        if measurement.name in self.protected_attributes:
            valid_attribute_name = measurement.name + "_cell"
            logger.warning(
                "{} is a protected attribute and cannot be set with this name "
                "in initialize_cell_attribute, changing name to {} and setting".format(
                    measurement.name, valid_attribute_name
                )
            )
            measurement.name = valid_attribute_name
        self.initialize_cell_attribute(measurement.name, measurement.data)
        setattr(self, measurement.columns_attr_name, np.asarray(measurement.columns))
        self.cell_measurements_col_mappings[
            measurement.name
        ] = measurement.columns_attr_name

    def initialize_gene_attribute(self, attribute_name, attribute):
        """Sets and registers a gene-wise attribute, e.g annotation information.
        """
        if (attribute_name in self.protected_attributes) or (
            attribute_name in self.cell_attribute_names
        ):
            valid_attribute_name = attribute_name + "_gene"
            logger.warning(
                "{} is a protected attribute or already exists as a cell attribute and cannot be set with this name "
                "in initialize_gene_attribute, changing name to {} and setting".format(
                    attribute_name, valid_attribute_name
                )
            )
            attribute_name = valid_attribute_name
        if not self.nb_genes == len(attribute):
            raise ValueError(
                "Number of genes ({n_genes}) and length of gene attribute ({n_attr}) mismatch".format(
                    n_genes=self.nb_genes, n_attr=len(attribute)
                )
            )
        setattr(self, attribute_name, np.asarray(attribute))
        self.gene_attribute_names.add(attribute_name)

    def initialize_mapped_attribute(
        self, source_attribute_name, mapping_name, mapping_values
    ):
        """Sets and registers an attribute mapping, e.g labels to named cell_types.
        """
        source_attribute = getattr(self, source_attribute_name)

        if isinstance(source_attribute, np.ndarray):
            type_source = source_attribute.dtype
        else:
            element = source_attribute[0]
            while isinstance(element, list):
                element = element[0]
            type_source = type(source_attribute[0])
        if not np.issubdtype(type_source, np.integer):
            raise ValueError(
                "Mapped attribute {attr_name} should be categorical not {type}".format(
                    attr_name=source_attribute_name, type=type_source
                )
            )
        cat_max = np.max(source_attribute)
        if not cat_max <= len(mapping_values):
            raise ValueError(
                "Max value for {attr_name} ({cat_max}) is higher than {map_name} ({n_map}) mismatch".format(
                    attr_name=source_attribute_name,
                    cat_max=cat_max,
                    map_name=mapping_name,
                    n_map=len(mapping_values),
                )
            )
        self.attribute_mappings[source_attribute_name].append(mapping_name)
        setattr(self, mapping_name, mapping_values)

    def compute_library_size_batch(self):
        """Computes the library size per batch."""
        self.local_means = np.zeros((self.nb_cells, 1))
        self.local_vars = np.zeros((self.nb_cells, 1))
        for i_batch in range(self.n_batches):
            idx_batch = np.squeeze(self.batch_indices == i_batch)
            (
                self.local_means[idx_batch],
                self.local_vars[idx_batch],
            ) = compute_library_size(self.X[idx_batch])
        self.cell_attribute_names.update(["local_means", "local_vars"])

    def collate_fn_builder(
        self,
        add_attributes_and_types: Dict[str, type] = None,
        override: bool = False,
        corrupted=False,
    ) -> Callable[[Union[List[int], np.ndarray]], Tuple[torch.Tensor, ...]]:
        """Returns a collate_fn with the requested shape/attributes
        """

        if override:
            attributes_and_types = dict()
        else:
            attributes_and_types = dict(
                [
                    ("X", np.float32) if not corrupted else ("corrupted_X", np.float32),
                    ("local_means", np.float32),
                    ("local_vars", np.float32),
                    ("batch_indices", np.int64),
                    ("labels", np.int64),
                ]
            )

        if add_attributes_and_types is None:
            add_attributes_and_types = dict()
        attributes_and_types.update(add_attributes_and_types)
        return partial(self.collate_fn_base, attributes_and_types)

    def collate_fn_base(
        self, attributes_and_types: Dict[str, type], batch: Union[List[int], np.ndarray]
    ) -> Tuple[torch.Tensor, ...]:
        """Given indices and attributes to batch, returns a full batch of ``Torch.Tensor``
        """
        indices = np.asarray(batch)
        data_numpy = [
            getattr(self, attr)[indices].astype(dtype)
            if isinstance(getattr(self, attr), np.ndarray)
            else getattr(self, attr)[indices].toarray().astype(dtype)
            for attr, dtype in attributes_and_types.items()
        ]

        data_torch = tuple(torch.from_numpy(d) for d in data_numpy)
        return data_torch

    #############################
    #                           #
    #      GENE FILTERING       #
    #                           #
    #############################

    def subsample_genes(
        self,
        new_n_genes: int = None,
        new_ratio_genes: Optional[float] = None,
        subset_genes: Optional[Union[List[int], List[bool], np.ndarray]] = None,
        mode: Optional[str] = "seurat_v3",
        batch_correction: Optional[bool] = True,
        **highly_var_genes_kwargs,
    ):
        """Wrapper around ``update_genes`` allowing for manual and automatic (based on count variance) subsampling.

        The function either:

            * Subsamples `new_n_genes` genes among all genes
            * Subsambles a proportion of `new_ratio_genes` of the genes
            * Subsamples the genes in `subset_genes`

        In the first two cases, a mode of highly variable gene selection is used as specified in the
        `mode` argument. F

        In the case where `new_n_genes`, `new_ratio_genes` and `subset_genes` are all None,
        this method automatically computes the number of genes to keep (when mode='seurat_v2'
        or mode='cell_ranger')

        In the case where `mode=="seurat_v3"`, an adapted version of the method described in [Stuart19]_
        is used. This method requires `new_n_genes` or `new_ratio_genes` to be specified.

        In the case where  `mode=="poisson_zeros"`, a method based on [Andrews & Hemberg 2019] is
        used. This method requires `new_n_genes` or `new_ratio_genes` to be specified.

        Parameters
        ----------
        subset_genes
            list of indices or mask of genes to retain
        new_n_genes
            number of genes to retain, the highly variable genes will be kept
        new_ratio_genes
            proportion of genes to retain, the highly variable genes will be kept
        mode
            Either "variance", "seurat_v2", "cell_ranger", "seurat_v3" or "poisson_zeros"
        batch_correction
            Account for batches when choosing highly variable genes.
            HVGs are selected in each batch and merged.
        highly_var_genes_kwargs
            Kwargs to feed to highly_variable_genes when using `seurat_v2`
            or `cell_ranger` (cf. highly_variable_genes method)
        """

        if subset_genes is None:

            # Converting ratio to new_n_genes if needed
            if new_ratio_genes is not None:
                if 0 < new_ratio_genes < 1:
                    new_n_genes = int(new_ratio_genes * self.nb_genes)
                else:
                    logger.info(
                        "Not subsampling. Expecting 0 < (new_ratio_genes={new_ratio_genes}) < 1.".format(
                            new_ratio_genes=new_ratio_genes
                        )
                    )
                    return

            # If new_n_genes is provided, assert that it has a proper value
            if new_n_genes is not None:
                if new_n_genes >= self.nb_genes or new_n_genes < 1:
                    logger.info(
                        "Not subsampling. Expecting: 1 < (new_n_genes={new_n_genes}) <= self.nb_genes".format(
                            new_n_genes=new_n_genes
                        )
                    )
                    return

            if mode == "variance":
                if new_n_genes is None:
                    logger.info(
                        "mode='variance' requires to specify new_n_genes or new_ratio_genes"
                    )
                    return
                std_scaler = StandardScaler(with_mean=False)
                std_scaler.fit(self.X.astype(np.float64))
                subset_genes = np.argsort(std_scaler.var_)[::-1][:new_n_genes]
            elif mode in ["seurat_v2", "cell_ranger", "seurat_v3", "poisson_zeros"]:
                genes_infos = self._highly_variable_genes(
                    n_top_genes=new_n_genes,
                    flavor=mode,
                    batch_correction=batch_correction,
                    **highly_var_genes_kwargs,
                )
                subset_genes = np.where(genes_infos["highly_variable"])[0]
            else:
                raise ValueError("Mode {mode} not implemented".format(mode=mode))

        self.update_genes(np.asarray(subset_genes))

    def filter_genes_by_attribute(
        self, values_to_keep: Union[List, np.ndarray], on: str = "gene_names"
    ):
        """Performs in-place gene filtering based on any gene attribute.
        Uses gene_names by default.
        """
        subset_genes = self._get_genes_filter_mask_by_attribute(
            attribute_values_to_keep=values_to_keep,
            attribute_name=on,
            return_data=False,
        )
        self.update_genes(subset_genes)

    def make_gene_names_lower(self):
        logger.info("Making gene names lower case")
        self.gene_names = np.char.lower(self.gene_names)

    def filter_genes_by_count(self, min_count: int = 1, per_batch: bool = False):
        if per_batch is True:
            batches = self.batch_indices.ravel()
            mask_genes_to_keep = np.ones((self.X.shape[1]))
            for b in np.unique(batches):
                mask_genes_to_keep = np.logical_and(
                    mask_genes_to_keep,
                    np.squeeze(
                        np.asarray(self.X[batches == b].sum(axis=0) >= min_count)
                    ),
                )
        else:
            mask_genes_to_keep = np.squeeze(np.asarray(self.X.sum(axis=0) >= min_count))

        self.update_genes(mask_genes_to_keep)

    def _get_genes_filter_mask_by_attribute(
        self,
        attribute_values_to_keep: Union[List, np.ndarray],
        attribute_name: str = "gene_names",
        return_data: bool = True,
    ):
        """Returns a mask with shape (nb_genes,) equal to ``True`` if the filtering condition is.

        Specifically, the mask is true if ``self.attribute_name`` is in ``attribute_values_to_keep``.

        Parameters
        ----------
        attribute_values_to_keep
            Values to accept for the filtering attribute.
        attribute_name
            Name of the attribute to filter genes on.
        return_data
            If True, returns the filtered data along with the mask.
        """
        if attribute_name not in self.gene_attribute_names:
            raise ValueError(
                "{name} is not a registered gene attribute".format(name=attribute_name)
            )

        attribute_values = getattr(self, attribute_name)
        subset_genes = np.isin(attribute_values, attribute_values_to_keep)

        if return_data:
            return self.X[:, subset_genes], subset_genes
        else:
            return subset_genes

    def update_genes(self, subset_genes: np.ndarray):
        """Performs a in-place sub-sampling of genes and gene-related attributes.

        Sub-selects genes according to ``subset_genes`` sub-index.
        Consequently, modifies in-place the data ``X`` and the registered gene attributes.

        Parameters
        ----------
        subset_genes
            Index used for gene sub-sampling.
            Either a ``int`` array with arbitrary shape which values are the indexes of the genes to keep.
            Or boolean array used as a mask-like index.
        """
        new_nb_genes = (
            len(subset_genes) if subset_genes.dtype != np.bool else subset_genes.sum()
        )
        logger.info(
            "Downsampling from {nb_genes} to {new_nb_genes} genes".format(
                nb_genes=self.nb_genes, new_nb_genes=new_nb_genes
            )
        )
        # update datasets
        self.X = self.X[:, subset_genes]
        for version_name in self.dataset_versions:
            data = getattr(self, version_name)
            setattr(self, version_name, data[:, subset_genes])

        # update gene-related attributes accordingly
        for attribute_name in self.gene_attribute_names:
            attr = getattr(self, attribute_name)
            setattr(self, attribute_name, attr[subset_genes])

        # remove non-expressing cells
        logger.info("Filtering non-expressing cells.")
        self.filter_cells_by_count()

    def reorder_genes(
        self,
        first_genes: Union[List[str], np.ndarray],
        drop_omitted_genes: bool = False,
    ):
        """Performs a in-place reordering of genes and gene-related attributes.

        Reorder genes according to the ``first_genes`` list of gene names.
        Consequently, modifies in-place the data ``X`` and the registered gene attributes.

        Parameters
        ----------
        first_genes
            New ordering of the genes; if some genes are missing, they will be added after the
            first_genes in the same order as they were before if `drop_omitted_genes` is False
        drop_omitted_genes
            Whether to keep or drop the omitted genes in `first_genes`
        """

        look_up = dict([(gene, index) for index, gene in enumerate(self.gene_names)])
        new_order_first = [look_up[gene] for gene in first_genes]

        if not drop_omitted_genes:
            new_order_second = [
                x for x in range(self.nb_genes) if x not in new_order_first
            ]
        else:
            new_order_second = []

        new_order = np.hstack([new_order_first, new_order_second]).astype(int)

        # update datasets
        self.X = self.X[:, new_order]
        for version_name in self.dataset_versions:
            data = getattr(self, version_name)
            setattr(self, version_name, data[:, new_order])

        # update gene-related attributes accordingly
        for attribute_name in self.gene_attribute_names:
            attr = getattr(self, attribute_name)
            setattr(self, attribute_name, attr[new_order])

    def genes_to_index(
        self, genes: Union[List[str], List[int], np.ndarray], on: str = None
    ):
        """Returns the index of a subset of genes, given their ``on`` attribute in ``genes``.

        If integers are passed in ``genes``, the function returns ``genes``.
        If ``on`` is None, it defaults to ``gene_names``.
        """
        if type(genes[0]) is not int:
            on = "gene_names" if on is None else on
            genes_idx = [np.where(getattr(self, on) == gene)[0][0] for gene in genes]
        else:
            genes_idx = genes
        return np.asarray(genes_idx, dtype=np.int64)

    #############################
    #                           #
    #      CELL FILTERING       #
    #                           #
    #############################

    def subsample_cells(self, size: Union[int, float] = 1.0):
        """Wrapper around ``update_cells`` allowing for automatic (based on sum of counts) subsampling.

        If size is a:

            * (0,1) float: subsample 100*``size`` % of the cells
            * int: subsample ``size`` cells
        """
        new_n_cells = (
            int(size * self.nb_cells)
            if not isinstance(size, (int, np.integer))
            else size
        )
        indices = np.argsort(np.squeeze(np.asarray(self.X.sum(axis=1))))[::-1][
            :new_n_cells
        ]
        self.update_cells(indices)

    def filter_cells_by_attribute(
        self, values_to_keep: Union[List, np.ndarray], on: str = "labels"
    ):
        """Performs in-place cell filtering based on any cell attribute.

        Uses labels by default.
        """
        subset_cells = self._get_cells_filter_mask_by_attribute(
            attribute_values_to_keep=values_to_keep,
            attribute_name=on,
            return_data=False,
        )
        self.update_cells(subset_cells)

    def filter_cells_by_count(self, min_count: int = 1):
        # squeezing necessary in case of sparse matrix
        mask_cells_to_keep = np.squeeze(np.asarray(self.X.sum(axis=1) >= min_count))
        self.update_cells(mask_cells_to_keep)

    def filter_cell_types(self, cell_types: Union[List[str], List[int], np.ndarray]):
        """Performs in-place filtering of cells by keeping cell types in ``cell_types``.

        Parameters
        ----------
        cell_types
            numpy array of type np.int (indices) or np.str (cell-types names)
        """
        cell_types = np.asarray(cell_types)
        if isinstance(cell_types[0], str):
            labels_to_keep = self.cell_types_to_labels(cell_types)
        elif isinstance(cell_types[0], (int, np.integer)):
            labels_to_keep = cell_types
        else:
            raise ValueError(
                "Wrong dtype for cell_types. Should be either str or int (labels)."
            )

        subset_cells = self._get_cells_filter_mask_by_attribute(
            attribute_name="labels",
            attribute_values_to_keep=labels_to_keep,
            return_data=False,
        )

        self.update_cells(subset_cells)

    def _get_cells_filter_mask_by_attribute(
        self,
        attribute_values_to_keep: Union[List, np.ndarray],
        attribute_name: str = "labels",
        return_data: bool = True,
    ):
        """Returns a mask with shape (nb_cells,) equal to ``True`` if the filtering condition is.

        Specifically, the mask is true if ``self.attribute_name`` is in ``attribute_values_to_keep``.

        Parameters
        ----------
        attribute_values_to_keep
            Values to accept for the filtering attribute.
        attribute_name
            Name of the attribute to filter cells on.
        return_data
            If True, returns the filtered data along with the mask.
        """
        if attribute_name not in self.cell_attribute_names:
            raise ValueError(
                "{name} is not a registered cell attribute".format(name=attribute_name)
            )

        attribute_values = getattr(self, attribute_name)
        subset_cells = np.isin(attribute_values, attribute_values_to_keep)
        # squeeze for labels and batch_indices which have an extra dim
        subset_cells = np.squeeze(subset_cells)

        if return_data:
            return self.X[subset_cells], subset_cells
        else:
            return subset_cells

    def update_cells(self, subset_cells):
        """Performs a in-place sub-sampling of cells and cell-related attributes.

        Sub-selects cells according to ``subset_cells`` sub-index.
        Consequently, modifies in-place the data ``X``, its versions and the registered cell attributes.

        Parameters
        ----------
        subset_cells
            Index used for cell sub-sampling.
            Either a ``int`` array with arbitrary shape which values are the indexes of the cells to keep.
            Or boolean array used as a mask-like index.
        """
        nb_cells_old = self.nb_cells

        # update cell-related attributes accordingly
        for attribute_name in self.cell_attribute_names:
            attr = getattr(self, attribute_name)
            setattr(self, attribute_name, attr[subset_cells])

        # subsample datasets which calls library_size_batch which requires already updated attributes
        self.X = self.X[subset_cells]
        for version_name in self.dataset_versions:
            data = getattr(self, version_name)
            setattr(self, version_name, data[subset_cells])

        logger.info(
            "Downsampled from {nb_cells} to {new_nb_cells} cells".format(
                nb_cells=nb_cells_old, new_nb_cells=self.nb_cells
            )
        )

    def merge_cell_types(
        self,
        cell_types: Union[
            Tuple[int, ...], Tuple[str, ...], List[int], List[str], np.ndarray
        ],
        new_cell_type_name: str,
    ):
        """Merges some cell types into a new one, and changes the labels accordingly.
        The old cell types are not erased but '#merged' is appended to their names

        Parameters
        ----------
        cell_types
            Cell types to merge.
        new_cell_type_name
            Name for the new aggregate cell type.
        """
        if not len(cell_types):
            raise ValueError("No cell types to merge.")
        if isinstance(cell_types[0], np.str):
            labels_subset = self.cell_types_to_labels(cell_types)
        else:
            labels_subset = np.asarray(cell_types)

        self.labels[np.isin(self.labels, labels_subset)] = len(self.cell_types)
        self.cell_types = np.concatenate([self.cell_types, [new_cell_type_name]])
        self.cell_types[labels_subset] = np.char.add(
            self.cell_types[labels_subset], "#merged"
        )

    def cell_types_to_labels(
        self, cell_types: Union[List[str], np.ndarray]
    ) -> np.ndarray:
        """Forms a one-on-one corresponding ``np.ndarray`` of labels for the specified ``cell_types``.
        """
        labels = [
            np.where(self.cell_types == cell_type)[0][0] for cell_type in cell_types
        ]
        return np.asarray(labels, dtype=np.int64)

    def map_cell_types(
        self,
        cell_types_dict: Dict[Union[int, str, Tuple[int, ...], Tuple[str, ...]], str],
    ):
        """Performs in-place filtering of cells using a cell type mapping.

        Cell types in the keys of ``cell_types_dict`` are merged and given the name of the associated value

        Parameters
        ----------
        cell_types_dict
            dictionary with tuples of cell types to merge as keys
            and new cell type names as values.
        """
        for cell_types, new_cell_type_name in cell_types_dict.items():
            self.merge_cell_types(cell_types, new_cell_type_name)

    def reorder_cell_types(self, new_order: Union[List[str], np.ndarray]):
        """Reorder in place the cell-types. The cell-types provided will be added at the beginning of `cell_types`
        attribute, such that if some existing cell-types are omitted in `new_order`, they will be left after the
        new given order
        """
        if isinstance(new_order, np.ndarray):
            new_order = new_order.tolist()

        for cell_type in self.cell_types:
            if cell_type not in new_order:
                new_order.append(cell_type)

        cell_types = OrderedDict([((x,), x) for x in new_order])
        self.map_cell_types(cell_types)
        self.remap_categorical_attributes(["labels"])

    #############################
    #                           #
    #           MISC.           #
    #                           #
    #############################

    def to_anndata(self) -> anndata.AnnData:
        """Converts the dataset to a anndata.AnnData object.
            The obtained dataset can then be saved/retrieved using the anndata API.
        """

        # obs/obsm contruction
        obsm = dict()
        obs = dict()
        for key in self.cell_attribute_names:
            if key not in ["local_means", "local_vars"]:
                vals = getattr(self, key)
                if key in self.cell_measurements_col_mappings:
                    obsm[key] = vals
                elif key == "labels" and self.cell_types is not None:
                    cell_types_arr = np.array(self.cell_types)
                    obs["cell_types"] = cell_types_arr[vals.squeeze()]
                else:
                    obs[key] = vals.squeeze()
        obs = pd.DataFrame(obs)

        # var contruction
        var = dict()
        for key in self.gene_attribute_names:
            if key != "gene_names":
                var[key] = getattr(self, key)
        gene_names = (
            self.gene_names if self.gene_names is not None else np.arange(self.nb_genes)
        )
        var = pd.DataFrame(var, index=gene_names)

        # It's important to save the measurements name mapping for latter loading
        all_names = {
            attr_name: getattr(self, attr_name)
            for _, attr_name in self.cell_measurements_col_mappings.items()
        }
        uns = dict(
            cell_measurements_col_mappings=self.cell_measurements_col_mappings,
            **all_names,
        )
        ad = anndata.AnnData(X=self.X, obs=obs, obsm=obsm, var=var, uns=uns)
        return ad

    def normalize(self):
        scaling_factor = self.X.mean(axis=1)
        self.norm_X = self.X / scaling_factor.reshape(len(scaling_factor), 1)

    def corrupt(self, rate: float = 0.1, corruption: str = "uniform"):
        """Forms a corrupted_X attribute containing a corrupted version of X.

        Sub-samples ``rate * self.X.shape[0] * self.X.shape[1]`` entries
        and perturbs them according to the ``corruption`` method.
        Namely:
            - "uniform" multiplies the count by a Bernouilli(0.9)
            - "binomial" replaces the count with a Binomial(count, 0.2)
        A corrupted version of ``self.X`` is stored in ``self.corrupted_X``.

        Parameters
        ----------
        rate
            Rate of corrupted entries.
        corruption
            Corruption method.
        """
        self.corrupted_X = copy.deepcopy(self.X)
        if (
            corruption == "uniform"
        ):  # multiply the entry n with a Ber(0.9) random variable.
            i, j = self.X.nonzero()
            ix = np.random.choice(len(i), int(np.floor(rate * len(i))), replace=False)
            i, j = i[ix], j[ix]
            self.corrupted_X[i, j] = np.squeeze(
                np.asarray(
                    np.multiply(
                        self.X[i, j],
                        np.random.binomial(n=np.ones(len(ix), dtype=np.int32), p=0.9),
                    )
                )
            )
        elif (
            corruption == "binomial"
        ):  # replace the entry n with a Bin(n, 0.2) random variable.
            i, j = (k.ravel() for k in np.indices(self.X.shape))
            ix = np.random.choice(len(i), int(np.floor(rate * len(i))), replace=False)
            i, j = i[ix], j[ix]
            self.corrupted_X[i, j] = np.squeeze(
                np.asarray(np.random.binomial(n=(self.X[i, j]).astype(np.int32), p=0.2))
            )
        else:
            raise NotImplementedError("Unknown corruption method.")

    def raw_counts_properties(
        self, idx1: Union[List[int], np.ndarray], idx2: Union[List[int], np.ndarray]
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Computes and returns some statistics on the raw counts of two sub-populations.

        Parameters
        ----------
        idx1
            subset of indices describing the first population.
        idx2
            subset of indices describing the second population.
        Returns
        -------
        type
            Tuple of ``np.ndarray`` containing, by pair (one for each sub-population),
            mean expression per gene, proportion of non-zero expression per gene, mean of normalized expression.

        """
        mean1 = (self.X[idx1, :]).mean(axis=0)
        mean2 = (self.X[idx2, :]).mean(axis=0)
        nonz1 = (self.X[idx1, :] != 0).mean(axis=0)
        nonz2 = (self.X[idx2, :] != 0).mean(axis=0)
        if self.norm_X is None:
            self.normalize()
        norm_mean1 = self.norm_X[idx1, :].mean(axis=0)
        norm_mean2 = self.norm_X[idx2, :].mean(axis=0)
        return (
            np.squeeze(np.asarray(mean1)),
            np.squeeze(np.asarray(mean2)),
            np.squeeze(np.asarray(nonz1)),
            np.squeeze(np.asarray(nonz2)),
            np.squeeze(np.asarray(norm_mean1)),
            np.squeeze(np.asarray(norm_mean2)),
        )

    def _highly_variable_genes(
        self,
        n_top_genes: int = None,
        flavor: Optional[str] = "seurat_v3",
        batch_correction: Optional[bool] = True,
        **highly_var_genes_kwargs,
    ) -> pd.DataFrame:
        """\
        Code adapted from the scanpy package
        Annotate highly variable genes [Satija15]_ [Zheng17]_ [Stuart19]_ [Andrews & Hemberg 2019].
        Depending on `flavor`, this reproduces the R-implementations of Seurat v2 and earlier
        [Satija15]_ and Cell Ranger [Zheng17]_, and Seurat v3 [Stuart19]_.

        Parameters
        ----------
        n_top_genes
            Number of highly-variable genes to keep. Mandatory for Seurat v3
        flavor
            Choose the flavor for computing normalized dispersion. One of "seurat_v2", "cell_ranger",
            "seurat_v3", or "poisson_zeros". In their default workflows, Seurat v2 passes the cutoffs
            whereas Cell Ranger and Poisson zeros passes `n_top_genes`.
        batch_correction
            Whether batches should be taken into account during procedure
        **highly_var_genes_kwargs
            Kwargs to feed to highly_variable_genes when using
            the Seurat V2 flavor.


        Returns
        -------
        type
            scanpy .var DataFrame providing genes information including means, dispersions
            and whether the gene is tagged highly variable (key `highly_variable`)
            (see scanpy highly_variable_genes documentation)

        """

        if flavor not in ["seurat_v2", "seurat_v3", "cell_ranger", "poisson_zeros"]:
            raise ValueError(
                "Choose one of the following flavors: 'seurat_v2', 'seurat_v3', 'cell_ranger', 'poisson_zeros'"
            )

        if flavor == "seurat_v3" and n_top_genes is None:
            raise ValueError("n_top_genes must not be None with flavor=='seurat_v3'")

        logger.info("extracting highly variable genes using {} flavor".format(flavor))

        # Creating AnnData structure
        obs = pd.DataFrame(
            data=dict(batch=self.batch_indices.squeeze()),
            index=np.arange(self.nb_cells),
        ).astype("category")

        counts = sp_sparse.csc_matrix(self.X.copy())
        adata = sc.AnnData(X=counts, obs=obs)
        batch_key = "batch" if (batch_correction and self.n_batches >= 2) else None
        if flavor in ["cell_ranger", "seurat_v2"]:
            if flavor == "seurat_v2":
                # name expected by scanpy
                flavor = "seurat"

            # Counts normalization
            sc.pp.normalize_total(adata, target_sum=1e4)
            # logarithmed data
            sc.pp.log1p(adata)

            # Finding top genes
            sc.pp.highly_variable_genes(
                adata=adata,
                n_top_genes=n_top_genes,
                flavor=flavor,
                batch_key=batch_key,
                inplace=True,  # inplace=False looks buggy
                **highly_var_genes_kwargs,
            )

        elif flavor == "seurat_v3":
            seurat_v3_highly_variable_genes(
                adata, n_top_genes=n_top_genes, batch_key=batch_key
            )

        elif flavor == "poisson_zeros":
            poisson_gene_selection(
                adata, n_top_genes, batch_key=batch_key, **highly_var_genes_kwargs
            )

        else:
            raise ValueError(
                "flavor should be one of 'seurat_v2', 'cell_ranger', 'seurat_v3', 'poisson_zeros'"
            )

        return adata.var

    def get_batch_mask_cell_measurement(self, attribute_name: str):
        """ Returns a list with length number of batches where each entry is a mask over present
            cell measurement columns

        Parameters
        ----------
        attribute_name
            cell_measurement attribute name

        Returns
        -------
        type
            List of ``np.ndarray`` containing, for each batch, a mask of which columns were
            actually measured in that batch. This is useful when taking the union of a cell measurement
            over datasets.

        """
        batch_mask = []
        for b in np.unique(self.batch_indices.ravel()):
            batch_sum = getattr(self, attribute_name)[
                np.where(self.batch_indices.ravel() == b)[0], :
            ].sum(axis=0)
            all_zero = batch_sum == 0
            batch_mask.append(~all_zero)

        return batch_mask


def poisson_gene_selection(
    adata,
    n_top_genes: int = 4000,
    use_cuda=True,
    n_samples: int = 10000,
    batch_key: str = None,
    silent: bool = False,
    minibatch_size: int = 5000,
    **kwargs,
):
    """ Rank and select genes based on the enrichment of zero counts in data
        compared to a Poisson count model.

        This is based on M3Drop: https://github.com/tallulandrews/M3Drop

        The method accounts for library size internally, a raw count matrix should be provided.

        Instead of Z-test, enrichment of zeros is quantified by posterior
        probabilites from a binomial model, computed through sampling.

        Writes columns in the adata.var DataFrame.

        Parameters
        ----------
        adata :
            AnnData object (with sparse X matrix).
        n_top_genes :
            How many variable genes to select.
        use_cuda :
            Default: ``False``.
        n_samples :
            The number of Binomial samples to use to estimate posterior probability
            of enrichment of zeros for each gene. Default: ``10000``.
        batch_key :
            key in adata.obs that contains batch info. If None, do not use batch info.
            Defatult: ``None``.
        silent :
            If ``True``, disables the progress bar. Default ``False``.
        minibatch_size :
            Size of temporary matrix for incremental calculation. Larger is faster but
            requires more RAM or GPU memory. (The default should be fine unless
            there are hundreds of millions cells or millions of genes.) Default ``5000``.

    """
    from tqdm import tqdm

    use_cuda = use_cuda and torch.cuda.is_available()
    dev = torch.device("cuda:0" if use_cuda else "cpu")

    if batch_key is None:
        batch_info = pd.Categorical(np.zeros(adata.shape[0], dtype=int))
    else:
        batch_info = adata.obs[batch_key]

    prob_zero_enrichments = []
    obs_frac_zeross = []
    exp_frac_zeross = []
    for b in np.unique(batch_info):

        X = adata[batch_info == b].X

        # Calculate empirical statistics.
        scaled_means = torch.from_numpy(X.sum(0) / X.sum())[0].to(dev)
        total_counts = torch.from_numpy(X.sum(1))[:, 0].to(dev)

        observed_fraction_zeros = torch.from_numpy(1.0 - (X > 0).sum(0) / X.shape[0])[
            0
        ].to(dev)

        # Calculate probability of zero for a Poisson model.
        # Perform in batches to save memory.
        minibatch_size = min(total_counts.shape[0], minibatch_size)
        n_batches = total_counts.shape[0] // minibatch_size

        expected_fraction_zeros = torch.zeros(scaled_means.shape).to(dev)

        for i in range(n_batches):
            total_counts_batch = total_counts[
                i * minibatch_size : (i + 1) * minibatch_size
            ]
            # Use einsum for outer product.
            expected_fraction_zeros += torch.exp(
                -torch.einsum("i,j->ij", [scaled_means, total_counts_batch])
            ).sum(1)

        total_counts_batch = total_counts[(i + 1) * minibatch_size :]
        expected_fraction_zeros += torch.exp(
            -torch.einsum("i,j->ij", [scaled_means, total_counts_batch])
        ).sum(1)
        expected_fraction_zeros /= X.shape[0]

        # Compute probability of enriched zeros through sampling from Binomial distributions.
        observed_zero = torch.distributions.Binomial(probs=observed_fraction_zeros)
        expected_zero = torch.distributions.Binomial(probs=expected_fraction_zeros)

        extra_zeros = torch.zeros(expected_fraction_zeros.shape).to(dev)
        for i in tqdm(
            range(n_samples),
            disable=silent,
            desc="Sampling from binomial",
            file=sys.stdout,
        ):
            extra_zeros += observed_zero.sample() > expected_zero.sample()

        prob_zero_enrichment = (extra_zeros / n_samples).cpu().numpy()

        obs_frac_zeros = observed_fraction_zeros.cpu().numpy()
        exp_frac_zeros = expected_fraction_zeros.cpu().numpy()

        # Clean up memory (tensors seem to stay in GPU unless actively deleted).
        del scaled_means
        del total_counts
        del expected_fraction_zeros
        del observed_fraction_zeros
        del extra_zeros

        if use_cuda:
            torch.cuda.empty_cache()

        prob_zero_enrichments.append(prob_zero_enrichment.reshape(1, -1))
        obs_frac_zeross.append(obs_frac_zeros.reshape(1, -1))
        exp_frac_zeross.append(exp_frac_zeros.reshape(1, -1))

    # Combine per batch results

    prob_zero_enrichments = np.concatenate(prob_zero_enrichments, axis=0)
    obs_frac_zeross = np.concatenate(obs_frac_zeross, axis=0)
    exp_frac_zeross = np.concatenate(exp_frac_zeross, axis=0)

    ranked_prob_zero_enrichments = prob_zero_enrichments.argsort(axis=1).argsort(axis=1)
    median_prob_zero_enrichments = np.median(prob_zero_enrichments, axis=0)

    median_obs_frac_zeross = np.median(obs_frac_zeross, axis=0)
    median_exp_frac_zeross = np.median(exp_frac_zeross, axis=0)

    median_ranked = np.median(ranked_prob_zero_enrichments, axis=0)

    num_batches_zero_enriched = np.sum(
        ranked_prob_zero_enrichments >= (adata.shape[1] - n_top_genes), axis=0
    )

    # Write results to adata.var

    adata.var["observed_fraction_zeros"] = median_obs_frac_zeross
    adata.var["expected_fraction_zeros"] = median_exp_frac_zeross
    adata.var["prob_zero_enriched_nbatches"] = num_batches_zero_enriched
    adata.var["prob_zero_enrichment"] = median_prob_zero_enrichments
    adata.var["prob_zero_enrichment_rank"] = median_ranked

    adata.var["highly_variable"] = False
    sort_columns = ["prob_zero_enriched_nbatches", "prob_zero_enrichment_rank"]
    top_genes = adata.var.nlargest(n_top_genes, sort_columns).index
    adata.var.loc[top_genes, "highly_variable"] = True


def seurat_v3_highly_variable_genes(
    adata, n_top_genes: int = 4000, batch_key: str = None
):
    """An adapted implementation of the "vst" feature selection in Seurat v3.

        This function will be replaced once it's full implemented in scanpy
        https://github.com/theislab/scanpy/pull/1182

        For further details of the arithmetic see https://www.overleaf.com/read/ckptrbgzzzpg

    Parameters
    ----------
    adata
        anndata object
    n_top_genes
        How many variable genes to return
    batch_key
        key in adata.obs that contains batch info. If None, do not use batch info

    """

    from scanpy.preprocessing._utils import _get_mean_var

    try:
        from skmisc.loess import loess
    except ImportError:
        raise ImportError(
            "Please install skmisc package via `pip install --user scikit-misc"
        )

    if batch_key is None:
        batch_info = pd.Categorical(np.zeros(adata.shape[0], dtype=int))
    else:
        batch_info = adata.obs[batch_key]

    norm_gene_vars = []
    for b in np.unique(batch_info):

        mean, var = _get_mean_var(adata[batch_info == b].X)
        not_const = var > 0
        estimat_var = np.zeros(adata.shape[1])

        y = np.log10(var[not_const])
        x = np.log10(mean[not_const])

        model = loess(x, y, span=0.3, degree=2)
        model.fit()
        estimat_var[not_const] = model.outputs.fitted_values
        reg_std = np.sqrt(10 ** estimat_var)

        batch_counts = adata[batch_info == b].X.astype(np.float64).copy()
        # clip large values as in Seurat
        N = np.sum(batch_info == b)
        vmax = np.sqrt(N)
        clip_val = reg_std * vmax + mean
        if sp_sparse.issparse(batch_counts):
            batch_counts = sp_sparse.csr_matrix(batch_counts)
            mask = batch_counts.data > clip_val[batch_counts.indices]
            batch_counts.data[mask] = clip_val[batch_counts.indices[mask]]
        else:
            clip_val_broad = np.broadcast_to(clip_val, batch_counts.shape)
            np.putmask(batch_counts, batch_counts > clip_val_broad, clip_val_broad)

        if sp_sparse.issparse(batch_counts):
            squared_batch_counts_sum = np.array(batch_counts.power(2).sum(axis=0))
            batch_counts_sum = np.array(batch_counts.sum(axis=0))
        else:
            squared_batch_counts_sum = np.square(batch_counts).sum(axis=0)
            batch_counts_sum = batch_counts.sum(axis=0)

        norm_gene_var = (1 / ((N - 1) * np.square(reg_std))) * (
            (N * np.square(mean))
            + squared_batch_counts_sum
            - 2 * batch_counts_sum * mean
        )
        norm_gene_vars.append(norm_gene_var.reshape(1, -1))

    norm_gene_vars = np.concatenate(norm_gene_vars, axis=0)
    # argsort twice gives ranks
    ranked_norm_gene_vars = np.argsort(np.argsort(norm_gene_vars, axis=1), axis=1)
    median_norm_gene_vars = np.median(norm_gene_vars, axis=0)
    median_ranked = np.median(ranked_norm_gene_vars, axis=0)

    num_batches_high_var = np.sum(
        ranked_norm_gene_vars >= (adata.X.shape[1] - n_top_genes), axis=0
    )
    df = pd.DataFrame(index=np.array(adata.var_names))
    df["highly_variable_nbatches"] = num_batches_high_var
    df["highly_variable_median_rank"] = median_ranked

    df["highly_variable_median_variance"] = median_norm_gene_vars
    df.sort_values(
        ["highly_variable_nbatches", "highly_variable_median_rank"],
        ascending=False,
        na_position="last",
        inplace=True,
    )
    df["highly_variable"] = False
    df.loc[:n_top_genes, "highly_variable"] = True
    df = df.loc[adata.var_names]

    adata.var["highly_variable"] = df["highly_variable"].values
    if len(np.unique(batch_info)) > 1:
        batches = adata.obs[batch_key].cat.categories
        adata.var["highly_variable_nbatches"] = df["highly_variable_nbatches"].values
        adata.var["highly_variable_intersection"] = df[
            "highly_variable_nbatches"
        ] == len(batches)
    adata.var["highly_variable_median_rank"] = df["highly_variable_median_rank"].values
    adata.var["highly_variable_median_variance"] = df[
        "highly_variable_median_variance"
    ].values


def remap_categories(
    original_categories: Union[List[int], np.ndarray],
    mapping_from: Union[List[int], np.ndarray] = None,
    mapping_to: Union[List[int], np.ndarray] = None,
    mappings_dict: Dict[str, Union[List[str], List[int], np.ndarray]] = None,
) -> Union[Tuple[np.ndarray, int], Tuple[np.ndarray, int, Dict[str, np.ndarray]]]:
    """Performs and returns a remapping of a categorical array.

    If None, ``mapping_from`` is set to np.unique(categories).
    If None, ``mapping_to`` is set to [0, ..., N-1] where N is the number of categories.
    Then, ``mapping_from`` is mapped to ``mapping_to``.

    Parameters
    ----------
    original_categories
        Categorical array to be remapped.
    mapping_from
        source of the mapping.
    mapping_to
        destination of the mapping
    mappings_dict
        Mappings of the categorical being remapped.

    Returns
    -------
    type
        tuple`` of a ``np.ndarray`` containing the new categories
        and an ``int`` equal to the new number of categories.

    """
    original_categories = np.asarray(original_categories)
    unique_categories = list(np.unique(original_categories))
    n_categories = len(unique_categories)
    if mapping_to is None:
        mapping_to = list(range(n_categories))
    if mapping_from is None:
        mapping_from = unique_categories

    # check lengths
    if n_categories > len(mapping_from):
        raise ValueError(
            "Size of provided mapping_from greater than the number of categories."
        )
    if len(mapping_to) != len(mapping_from):
        raise ValueError("Length mismatch between mapping_to and mapping_from.")

    new_categories = np.copy(original_categories)
    for cat_from, cat_to in zip(mapping_from, mapping_to):
        new_categories[original_categories == cat_from] = cat_to
    new_categories = new_categories.astype(np.uint16)
    unique_new_categories = np.unique(new_categories)
    if mappings_dict is not None:
        new_mappings = {}
        for mapping_name, mapping in mappings_dict.items():
            new_mapping = np.empty(
                unique_new_categories.shape[0], dtype=np.asarray(mapping).dtype
            )
            for cat_from, cat_to in zip(mapping_from, mapping_to):
                new_mapping[cat_to] = mapping[cat_from]
            new_mappings[mapping_name] = new_mapping
        return new_categories, n_categories, new_mappings
    else:
        return new_categories, n_categories


def compute_library_size(
    data: Union[sp_sparse.csr_matrix, np.ndarray]
) -> Tuple[np.ndarray, np.ndarray]:
    sum_counts = data.sum(axis=1)
    masked_log_sum = np.ma.log(sum_counts)
    if np.ma.is_masked(masked_log_sum):
        logger.warning(
            "This dataset has some empty cells, this might fail scVI inference."
            "Data should be filtered with `my_dataset.filter_cells_by_count()"
        )
    log_counts = masked_log_sum.filled(0)
    local_mean = (np.mean(log_counts).reshape(-1, 1)).astype(np.float32)
    local_var = (np.var(log_counts).reshape(-1, 1)).astype(np.float32)
    return local_mean, local_var


def concatenate_arrays(arrays):
    # concatenate data
    if all([type(array) is np.ndarray for array in arrays]):
        concatenation = np.concatenate(arrays)
    # if sparse, cast all to sparse and stack
    else:
        concatenation = sp_sparse.vstack(
            [
                array
                if isinstance(array, sp_sparse.csr_matrix)
                else sp_sparse.csr_matrix(array)
                for array in arrays
            ]
        )
    return concatenation


def check_nonnegative_integers(X: Union[np.ndarray, sp_sparse.csr_matrix]):
    """Checks values of X to ensure it is count data
    """

    data = X if type(X) is np.ndarray else X.data
    # Check no negatives
    if np.any(data < 0):
        return False
    # Check all are integers
    elif np.any(~np.equal(np.mod(data, 1), 0)):
        return False
    else:
        return True


class DownloadableDataset(GeneExpressionDataset, ABC):
    """Sub-class of ``GeneExpressionDataset`` which downloads its data to disk and
    then populates its attributes with it.

    In particular, it has a ``delayed_populating`` parameter allowing for instantiation
    without populating the attributes.

    Parameters
    ----------
    urls
        single or multiple url.s from which to download the data.
    filenames
        filenames for the downloaded data.
    save_path
        path to data storage.
    delayed_populating
        If False, populate object upon instantiation.
        Else, allow for a delayed manual call to ``populate`` method.

    """

    def __init__(
        self,
        urls: Union[str, Iterable[str]] = None,
        filenames: Union[str, Iterable[str]] = None,
        save_path: str = "data/",
        delayed_populating: bool = False,
    ):
        super().__init__()
        if isinstance(urls, str):
            self.urls = [urls]
        elif urls is None:
            self.urls = []
        else:
            self.urls = urls
        if isinstance(filenames, str):
            self.filenames = [filenames]
        elif filenames is None:
            self.filenames = [
                "dataset_{i}".format(i=i) for i, _ in enumerate(self.urls)
            ]
        else:
            self.filenames = filenames

        self.save_path = os.path.abspath(save_path)
        self.download()
        if not delayed_populating:
            self.populate()

    def download(self):
        for url, download_name in zip(self.urls, self.filenames):
            _download(url, self.save_path, download_name)

    @abstractmethod
    def populate(self):
        """Populates a ``DonwloadableDataset`` object's data attributes.

        E.g by calling one of ``GeneExpressionDataset``'s ``populate_from...`` methods.
        """
        pass


def _download(url: str, save_path: str, filename: str):
    """Writes data from url to file.
    """
    if os.path.exists(os.path.join(save_path, filename)):
        logger.info("File %s already downloaded" % (os.path.join(save_path, filename)))
        return
    try:
        r = urllib.request.urlopen(url)
    except HTTPError:
        req = urllib.request.Request(url, headers={"User-Agent": "Magic Browser"})
        r = urllib.request.urlopen(req)
    logger.info("Downloading file at %s" % os.path.join(save_path, filename))

    def read_iter(file, block_size=1000):
        """Given a file 'file', returns an iterator that returns bytes of
        size 'blocksize' from the file, using read().
        """
        while True:
            block = file.read(block_size)
            if not block:
                break
            yield block

    # Create the path to save the data
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    with open(os.path.join(save_path, filename), "wb") as f:
        for data in read_iter(r):
            f.write(data)
