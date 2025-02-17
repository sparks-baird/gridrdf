"""
Module to compute earth mover's distance (EMD) for different inputs.

Allows EMD comparison between the following features:
- Composition (as normalised vector)
- GRID histograms (mean of pairwise comparisons)
- RDF

The module also includes helper functions to compute dissimilarity matrices
between multiple input pairs, for later ML.
"""

import sys
import os
import json
import math
from typing import Union
import numpy as np
import pandas as pd
import argparse
from tqdm import tqdm

try:
    from pymatgen import Structure
except ImportError:
    from pymatgen.core.structure import Structure
try:
    from pymatgen import Lattice
except ImportError:
    from pymatgen.core.lattice import Lattice
from pymatgen.analysis.structure_matcher import StructureMatcher
from scipy import spatial
from scipy.sparse import coo_matrix

import warnings

# Used for faster version of EMD
import numba as nb

# the wasserstein distance in scipy treats frequencies of each bin as a value,
# and then builds the distributions from those values and computes the distance.
# You can either simply pass the values that you create histograms from
# or pass mid points of bins as values and frequencies as weights
# here the later method is used
from scipy.stats import wasserstein_distance

# generally the wasserstein distance in scipy is much faster than EMD package
# these two have been checked, they give same results
# so this emd is used only when
#   1. the flow between two distribution is needed
#   2. a distance matrix need to be defined, like composition similarity
from pyemd import emd, emd_with_flow


from .data_io import rdf_read, rdf_read_parallel
from .data_explore import rdf_trim, rdf_flatten
import gridrdf.composition
from .composition import composition_one_hot, element_indice
from .misc import int_or_str


def emd_formula_example():
    """
    Test the Earth Mover's Distance (EMD) using similarity matrix
    against the EMD in the literature
    https://github.com/lrcfmd/ElMD/
    """

    try:
        from ElMD import ElMD
    except:
        print("Element-EMD module not installed")

    elem_emd = ElMD()

    comp1 = elem_emd._gen_vector("Li0.7Al0.3Ti1.7P3O12")
    comp2 = elem_emd._gen_vector("La0.57Li0.29TiO3")
    pettifor_emd = elem_emd._EMD(comp1, comp2)

    comp1_reindex = pd.DataFrame(
        comp1, index=gridrdf.composition.modified_pettifor
    ).reindex(index=gridrdf.composition.pettifor)
    comp2_reindex = pd.DataFrame(
        comp2, index=gridrdf.composition.modified_pettifor
    ).reindex(index=gridrdf.composition.pettifor)

    dist_matrix = pd.read_csv("similarity_matrix.csv", index_col="ionA").values
    dist_matrix = dist_matrix.copy(order="C")

    em = emd_with_flow(
        comp1_reindex.values[:, 0], comp2_reindex.values[:, 0], dist_matrix
    )
    simi_matrix_emd = em[0]
    emd_flow = pd.DataFrame(
        em[1], columns=gridrdf.composition.pettifor, index=gridrdf.composition.pettifor
    )
    emd_flow.replace(0, np.nan).to_csv("emd_flow.csv")


def find_same_structure(data):
    """
    Find all pairs of structures that give exactly the same extended RDF
    in a dataset

    Args:
        data: json data containing structures information and MP-ID(typically CIFs)
    Return:
        A nested list of MP-IDs with same structures, grouped by space group number
    """
    # group the structures (without composition information) using space group
    # first make a dict with keys are space group numbers
    sg_grouped_structs = {}
    for sg_num in range(230, 0, -1):
        sg_grouped_structs[sg_num] = {}

    for d in data:
        struct = Structure.from_str(d["cif"], fmt="cif")
        # remove the composition information by setting all elements to X
        # as currently extended RDF does not have composition information
        for elem in struct.symbol_set:
            struct[elem] = "H"

        sg_num = struct.get_space_group_info()[1]
        sg_grouped_structs[sg_num][d["task_id"]] = struct

    sc = StructureMatcher(ltol=0.05, stol=0.05, scale=False)
    match_list = {}
    for sg_num, structs in sg_grouped_structs.items():
        print(sg_num, len(structs), flush=True)
        match_list[sg_num] = []

        while len(structs) != 0:
            struct_1 = list(structs.values())[0]
            same_struct_list = {}
            vol_per_atom_1 = struct_1.volume / struct_1.num_sites
            for task_id_2, struct_2 in structs.items():
                vol_per_atom_2 = struct_2.volume / struct_2.num_sites
                if abs(vol_per_atom_1 - vol_per_atom_2) < 1:
                    if sc.fit(struct_1, struct_2):
                        struct_2_latt = struct_2.get_primitive_structure().lattice.a
                        same_struct_list[task_id_2] = [vol_per_atom_2, struct_2_latt]
            if len(same_struct_list) > 1:
                match_list[sg_num].append(same_struct_list)
            for mp_id in same_struct_list:
                structs.pop(mp_id)

        """
        if len(structs) > 0:
            match_list[sg_num] = []
            for i, (task_id_1, struct_1) in enumerate(tqdm(structs.items(), mininterval=60)):
                vol_per_atom_1 = struct_1.volume / struct_1.num_sites
                for j, (task_id_2, struct_2) in enumerate(structs.items()):
                    vol_per_atom_2 = struct_2.volume / struct_2.num_sites
                    if i < j and abs(vol_per_atom_1 - vol_per_atom_2) < 1:
                        if sc.fit(struct_1, struct_2):
                            match_list[sg_num].append([task_id_1, task_id_2])
                            print(task_id_1, task_id_2)
                            break
        """
    return match_list


def find_same_rdf(all_rdf, data):
    """
    Find all exactly the same extended RDF in a dataset

    Args:
        all_rdf: all the trimmed and flatten rdf
        data: json data containing structures information and MP-ID
    Return:
        A list of pairs of MP-IDs
    """
    # make all_rdf a dict with keys of mp-ids
    rdfs = {}
    for i, d in enumerate(data):
        rdfs[d["task_id"]] = all_rdf[i]

    match_list = []
    while len(rdfs) != 0:
        rdf_1 = list(rdfs.values())[0]
        same_rdf_list = []
        for task_id_2, rdf_2 in rdfs.items():
            if np.array_equal(rdf_1, rdf_2):
                same_rdf_list.append(task_id_2)
        if len(same_rdf_list) > 1:
            match_list.append(same_rdf_list)
        for mp_id in same_rdf_list:
            rdfs.pop(mp_id)
    return match_list


def analysis_emd_100():
    """ """
    df = pd.DataFrame([], index=np.linspace(0.2, 0.6, 5))
    for i in ["small", "middle", "large"]:
        for thresh in np.linspace(0.2, 0.6, 5):
            data = np.loadtxt(i + "_sample_" + str(thresh), delimiter=" ")
            for val in range(4):
                df.loc[thresh, i + str(val)] = np.count_nonzero(data == val)
    df.to_csv("analysis.csv")


def dist_matrix_1d(nbin=100):
    """
    Generate distance matrix for earth mover's distance use
    """

    # dist_matrix = np.abs(np.subtract.outer(range(nbin), range(nbin)))
    dist_matrix = pd.DataFrame()
    for x in range(nbin):
        for y in range(nbin):
            dist_matrix.loc[x, y] = abs(x - y)
    return dist_matrix.values


def nn_bulk_modulus_single(baseline_id, data, n_nn=1, emd="both"):
    """
    Using the calucated EMD values and find the n nearest neighbor to estimate
    bulk modulus

    Args:
        baseline_id: the mp task_id need to be estimated
        data: mp json data from file
        n_nn: number of nearest neighbors
        emd: the emd values used
            rdf: only rdf emd
            compos: only composition emd
            both:
    Return:
        The baseline mp-id
        Ground truth of bulk modluls
        mp-id of the nearest neighbor
        Estimation of the bulk modulus
        EMD distance between the nearest neighbor and baseline
    """
    df_mp = pd.DataFrame.from_dict(data)
    df_mp = df_mp.set_index("task_id")

    if emd == "rdf" or emd == "both":
        rdf_emd_file = os.path.join(
            sys.path[0], "../rdf_emd/", baseline_id + "_emd.csv"
        )
        rdf_emd = pd.read_csv(rdf_emd_file, index_col=0)

    if emd == "compos" or emd == "both":
        compos_emd_file = os.path.join(
            sys.path[0], "../compos_emd/", baseline_id + "_compos_emd.csv"
        )
        compos_emd = pd.read_csv(compos_emd_file, index_col=0)

    # now add two emds to give a finally emd, need new method
    if emd == "both":
        total_emd = rdf_emd.add(compos_emd)
    elif emd == "rdf":
        total_emd = rdf_emd
    elif emd == "compos":
        total_emd = compos_emd

    nn_mps = total_emd.nsmallest(n_nn + 1, baseline_id)

    # sometimes there are other structures have zero emd with
    # the baseline_id, so it is not always the first row is the baseline_id
    # so use drop instead of removing the first row
    try:
        nn_mps = nn_mps.drop(baseline_id)
    except:
        # sometimes even the baseline_id may not be in the n-nearest
        nn_mps = nn_mps[1:]

    aver_modul = 0
    for task_id in nn_mps.index.values:
        aver_modul += df_mp["elasticity.K_VRH"][task_id]

    return (
        baseline_id,
        df_mp["elasticity.K_VRH"][baseline_id],
        nn_mps.index.values[0],
        aver_modul / n_nn,
        nn_mps.values[0][0],
    )


def nn_bulk_modulus_matrix_add(
    data, nn=1, simi_dir=".", simi_matrix=["extended_rdf_emd"], scale=True
):
    """
    Deprecated. The adding similarity matrix is worse than currently used
    Using the calucated EMD values and find the n nearest neighbor to estimate
    bulk modulus. If two EMD matrix are used, then add the values of the two matrix

    Args:
        data: mp json data from file
        nn: number of nearest neighbors
        matrix: a list of the emd values used
            extended_rdf: emd of extended rdf
            original_rdf:
            composition: only composition emd
        scale: no implement yet
    Return:
        The baseline mp-id
        Ground truth of bulk modluls
        Estimation of the bulk modulus
        EMD distance between the nearest neighbor and baseline
        mp-id of the nearest neighbor
    """

    warnings.warn(
        "`nn_bulk_modulus_matrix_add` is deprecated and will be removed",
        DeprecationWarning,
    )

    # read the modulus data and convert to pandas dataframe
    # for the usage of later ground truth and mp ids
    df_mp = pd.DataFrame.from_dict(data)
    df_mp = df_mp.set_index("task_id")

    # read the emd matrix
    total_emd = pd.DataFrame(
        np.zeros((len(data), len(data))), index=df_mp.index, columns=df_mp.index
    )
    if "extended_rdf_emd" in simi_matrix:
        rdf_emd_file = os.path.join(simi_dir, "extended_rdf_emd.csv")
        rdf_emd = pd.read_csv(rdf_emd_file, index_col=0)
        rdf_emd = rdf_emd.fillna(0).add(rdf_emd.transpose().fillna(0))
        if scale:
            total_emd = total_emd.add(rdf_emd * 10)
        else:
            total_emd = total_emd.add(rdf_emd)

    if "extended_rdf_cosine" in simi_matrix:
        rdf_emd_file = os.path.join(simi_dir, "extended_rdf_cosine.csv")
        rdf_emd = pd.read_csv(rdf_emd_file, index_col=0)
        rdf_emd = rdf_emd.fillna(0).add(rdf_emd.transpose().fillna(0))
        total_emd = total_emd.add(rdf_emd)

    if "composition_emd" in simi_matrix:
        compos_emd_file = os.path.join(simi_dir, "composition_emd.csv")
        compos_emd = pd.read_csv(compos_emd_file, index_col=0)
        compos_emd = compos_emd.fillna(0).add(compos_emd.transpose().fillna(0))
        total_emd = total_emd.add(compos_emd)

    if total_emd.isna().all().sum() > 0:
        raise IndexError("data file is a different size to distance matrix")

    pred_k = pd.DataFrame(
        index=df_mp.index,
        columns=[
            "ground_truth",
            "predict_bulk_modulus",
            "smallest_emd",
            "nearest_neighbor",
        ],
    )
    for baseline_id in df_mp.index.values:
        nn_mps = total_emd[baseline_id].drop(baseline_id).nsmallest(nn)

        aver_modul = 0
        for task_id in nn_mps.index.values:
            aver_modul += df_mp["elasticity.K_VRH"][task_id]

        # df.loc index go first, df[] column go first
        pred_k.loc[baseline_id]["predict_bulk_modulus"] = aver_modul / nn
        pred_k.loc[baseline_id]["ground_truth"] = df_mp["elasticity.K_VRH"][baseline_id]
        pred_k.loc[baseline_id]["smallest_emd"] = nn_mps.values[0]
        pred_k.loc[baseline_id]["nearest_neighbor"] = nn_mps.index.values[0]

    return pred_k


def nn_bulk_modulus_matrix_step(
    data, simi_matrix=["extended_rdf_emd", "composition_emd"]
):
    """
    Using the calucated EMD values and find the n nearest neighbor to estimate
    bulk modulus

    If two similaity matrix is used, first search the nearest neighbors of the first matrix,
    if same values are found, then use the second matrix to find which is the nearest

    Args:
        data: mp json data from file
        emd: a list of the emd values used, the first one will be used for the
            first nearest neighbor search. values:
                extended_rdf: emd of extended rdfs
                extended_rdf_cos: cosine similarity of the extended rdfs
                composition: only composition emd
    Return:
        The baseline mp-id
        Ground truth of bulk modluls
        Estimation of the bulk modulus
        EMD distance between the nearest neighbor and baseline
        mp-id of the nearest neighbor
    """
    # read the modulus data and convert to pandas dataframe
    # for the usage of later ground truth and mp ids
    df_mp = pd.DataFrame.from_dict(data)
    df_mp = df_mp.set_index("task_id")

    # read the emd matrix
    total_emd = pd.DataFrame(
        np.zeros((len(data), len(data))), index=df_mp.index, columns=df_mp.index
    )

    file_1 = os.path.join(sys.path[0], simi_matrix[0] + ".csv")
    simi_mat_1 = pd.read_csv(file_1, index_col=0)
    simi_mat_1 = simi_mat_1.fillna(0).add(simi_mat_1.transpose().fillna(0))

    file_2 = os.path.join(sys.path[0], simi_matrix[1] + ".csv")
    simi_mat_2 = pd.read_csv(file_2, index_col=0)
    simi_mat_2 = simi_mat_2.fillna(0).add(simi_mat_2.transpose().fillna(0))

    pred_k = pd.DataFrame(
        index=df_mp.index,
        columns=[
            "ground_truth",
            "predict_bulk_modulus",
            "emd_1",
            "emd_2",
            "nearest_neighbor",
        ],
    )
    for baseline_id in df_mp.index.values:
        single_emd = simi_mat_1[baseline_id].drop(baseline_id)
        # fount the indice with the smallest value
        nn_idx = single_emd.where(single_emd == single_emd.min()).dropna()
        nn_idx = nn_idx.to_frame()

        if len(nn_idx) > 1:
            # if there are multiple structures have the same end,
            # then determine the nearest by considering the second similarity matrix
            new_list = []
            for task_id in nn_idx.index.values:
                new_list.append(simi_mat_2[baseline_id][task_id])
            nn_idx["new"] = new_list
            nn_mps = nn_idx.nsmallest(1, "new")
        else:
            nn_idx["new"] = [0]
            nn_mps = nn_idx

        # df.loc index go first, df[] column go first
        pred_k.loc[baseline_id]["predict_bulk_modulus"] = df_mp["elasticity.K_VRH"][
            nn_mps.index.values[0]
        ]
        pred_k.loc[baseline_id]["ground_truth"] = df_mp["elasticity.K_VRH"][baseline_id]
        pred_k.loc[baseline_id]["emd_1"] = nn_mps.values[0][0]
        pred_k.loc[baseline_id]["emd_2"] = nn_mps.values[0][1]
        pred_k.loc[baseline_id]["nearest_neighbor"] = nn_mps.index.values[0]

    return pred_k


def composition_similarity(baseline_id, data, index="z_number_78"):
    """
    Calcalate the earth mover's distance between the baseline structure and all others
    in the dataset

    Args:
        data: a pandas dataframe, index is mp-ids, and columns is element symbols
    Return:
        a pandas dataframe of pairwise distance between baseline id and all others
    """
    # define the indice by call element_indice function
    element_indice()

    dist = []
    elem_similarity_file = os.path.join(sys.path[0], "similarity_matrix.csv")
    dist_matrix = pd.read_csv(elem_similarity_file, index_col="ionA")
    dist_matrix = 1 / (np.log10(1 / dist_matrix + 1))

    if index == "pettifor":
        dist_matrix = dist_matrix.reindex(
            columns=gridrdf.composition.pettifor, index=gridrdf.composition.pettifor
        )
    # the earth mover's distance package need order C and float64 astype('float64')
    dist_matrix = dist_matrix.values.copy(order="C")

    compo_emd = pd.DataFrame(columns=[baseline_id])
    mp_id_1 = baseline_id
    for mp_id_2 in tqdm(data.index, mininterval=60):
        compo_emd.loc[mp_id_2] = emd(
            data.loc[mp_id_1].values.copy(order="C"),
            data.loc[mp_id_2].values.copy(order="C"),
            dist_matrix,
        )
    return compo_emd


def composition_similarity_matrix(
    data,
    indice=None,
    index="z_number_78",
    elem_similarity: Union[pd.DataFrame, str] = "similarity_matrix.csv",
):
    """
    Calcalate pairwise earth mover's distance of all compositions, the composition should be
    a 78-element vector, as the elemental similarity_matrix is 78x78 matrix in the order of
    atom numbers

    Args:
        data: pandas dataframe of element vectors for all the structures
        index: see function element_indice for details
            z_number_78: (default) in the order of atomic number, this is default
                because the similarity matrix is in this order
            z_number: see periodic_table in function element_indice
            pettifor: see function element_indice for details
            modified_pettifor: see element_indice
            elem_present: the vector only contain the elements presented in the dataset
    Return:
        a pandas dataframe of pairwise EMD with mp-ids as index
    """
    # define the indice by call element_indice function
    element_indice()

    # if indice is None, then loop over the whole dataset
    if not indice:
        indice = [0, len(data)]

    dist = []
    if isinstance(elem_similarity, str):
        dist_matrix = pd.read_csv(elem_similarity, index_col="ionA")
    else:
        dist_matrix = elem_similarity
    dist_matrix = 1 / (np.log10(1 / dist_matrix + 1))

    if index == "pettifor":
        dist_matrix = dist_matrix.reindex(
            columns=gridrdf.composition.pettifor, index=gridrdf.composition.pettifor
        )
    # the earth mover's distance package need order C and float64 astype('float64')
    dist_matrix = dist_matrix.values.copy(order="C")

    ids = data.iloc[indice[0] : indice[1]].index
    compo_emd = pd.DataFrame(np.zeros((indice[-1], indice[-1])), index=ids, columns=ids)
    for i1 in range(indice[0], indice[1]):
        mp_id_1 = data.index[i1]
        for i2, mp_id_2 in enumerate(data.index):
            if i1 <= i2:
                emd_value = emd(
                    data.loc[mp_id_1].values.copy(order="C"),
                    data.loc[mp_id_2].values.copy(order="C"),
                    dist_matrix,
                )
                compo_emd.loc[mp_id_1, mp_id_2] = emd_value
            else:
                compo_emd.loc[mp_id_1, mp_id_2] = np.nan
    return compo_emd


@nb.njit(parallel=True)
def _emd_cumsum_row(cumsum_grid_array, index, bin_width, weights=None):
    """Super-fast implementation of EMD with Numba support for an array of structures.

    WARNING: This algorithm does not do any error checking! It assumes that the input is
             a NumPy array of cumulative, normalised grid distributions.

    This is optimised to calculate EMD between one GRID and many others in parallel, with
    the aim to create a 2D EMD distance matrix. As such, it only computes EMD to structures
    with index greater or equal to `index`.


    Parameters
    ----------

    cumsum_grid_array: np.ndarray, shape (nstructures, ngrid_shells, nbins)
        Cumulative distributions of normalised GRIDs, stacked into a single 3D tensor.
        IMPORTANT: cumulative distributions must all sum to the same value!

    index: int
        Index of structure to calculate pairwise EMDs for
    bin_width : float
        Width of each histogram bin, used to normalise the resulting EMD correctly
    weights: array-like, default None
        Weighting to apply to each grid shell before computing the mean. If None, equal weighting (1/n_shells)
        will be applied.
        The weights should be the same length as the number of GRID shells, and should sum to the number of shells (not 1).


    Notes
    -----

    As detailed [scipy.stats.wasserstein_distance](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.wasserstein_distance.html),
    an equivalent definition of EMD based on cumulative distribution functions is:

    $$
    EMD(u,v) = \int_{-\infty}{+\infty}|U - V|
    $$
    where $u,v$ are probability distributions and $U, V$ are their respectives cumulative distributions. As such, the EMD between
    two GRID representations $(G,H)$ will be
    $$
    EMD(G, H) = \sum_{j=1}^n \left( \frac{\sum_i | G_{j,i} - H_{j,i} |}{max(U)} \times w \right) / n
    $$
    where the sum over $i$ is for all bins in a given GRID shell (to the same cutoff) and $w$ is the width of each bin. The
    division by $max(U)$ is needed in cases where $u$ and $v$ are not normalised to sum to 1, for instance when $w < 1$.
    $j$ is the sum across all GRID shells $n$, meaning that the overall EMD is the mean of each shell EMD (currently, this
    may change in future).

    """

    if weights is None:
        shell_weights = np.ones(cumsum_grid_array.shape[1])
    else:
        # Avoid numba type errors by simply multiplying ones by weights (also checks array sizes)
        shell_weights = np.ones(cumsum_grid_array.shape[1]) * weights

    output = np.zeros(cumsum_grid_array.shape[0])
    for j in nb.prange(index, cumsum_grid_array.shape[0]):
        output[j] = np.mean(
            shell_weights
            * np.sum(np.abs(cumsum_grid_array[index] - cumsum_grid_array[j]), axis=-1)
            / np.max(cumsum_grid_array[index])
            * bin_width
        )
        # output[j] = np.sum(shell_weights)
    return output


@nb.njit()
def _EMD_cumsum(cumulative_grid1, cumulative_grid2, bin_width, weights=None):
    """
    Return EMD between two cumulative GRID (2D) representations.

    Parameters
    ----------

    cumulative_grid1, cumulative_grid2: array-like
        2D arrays containing cumulative distribution values with which to calculate Earth Mover's Distance
    bin_width: float
        Width of each bin in order to normalise the EMD correctly
    weights: array-like, default None
        Weighting to apply to each grid shell before computing the mean. If None, equal weighting (1/n_shells)
        will be applied.
        The weights should be the same length as the number of GRID shells, and should sum to 1.

    Returns
    -------

    EMD : float
        Earth mover distance between two GRID distributions

    Notes
    -----

    For mathematical explanation see `_EMD_cumsum_row`

    """

    if weights is None:
        weights = np.ones(cumulative_grid1.shape[0])
    return np.mean(
        weights
        * np.sum(np.abs(cumulative_grid1 - cumulative_grid2), axis=-1)
        / np.max(cumulative_grid1)
        * bin_width
    )


@nb.njit()
def _flattened_EMD_cumsum(
    cumulative_grid_flat1, cumulative_grid_flat2, shape, bin_width, weights=None
):
    """
    Compute EMD between two flattened GRID distributions.
    """

    grid1 = cumulative_grid_flat1.reshape(shape)
    grid2 = cumulative_grid_flat2.reshape(shape)

    return _EMD_cumsum(grid1, grid2, bin_width, weights)


def super_fast_EMD_matrix(
    grids,
    bin_width,
    weighting="constant",
    weighting_kwargs={"n": 0.0},
    results_array=None,
):
    """
    Efficient parallelised method to compute the matrix of EMD for a collection of GRID representations.

    Warning - for large lists of GRIDS, this can be very memory (and compute) intensive!

    Parameters
    ----------
    grids : array-like
        Iterable of GRID representations
    bin_width : float
        Width of each GRID bin for correctly normalising EMD
    weighting : str or function
        Weighting to apply to each grid shell when computing the overall EMD, allowing
        biasing towards short- or long-range similarity. A string argument will use
        a pre-defined weighting function, or alternatively a callable can be provided in
        order to compute weightings. If callable, the function should take the number of
        shells as input and return an array of values.
        Note that weights should be normalised to sum to the number
        of GRID shells (not 1).
        In-built options:
            'constant': equal weighting to all GRID shells
            'power' : weighting of shell x is 1/x**n (normalised over all shells)
        Default : 'constant' (equal weighting to bins)
    weighting_kwargs : dict
        Kwargs to use for computing weighting, either as parameters to in-built
        functions or to be passed direct to a weighting callable.
    results_array : array-like, default None
        If not None, EMD values will be stored to this array. It should
        be square with shape (len(grids), len(grids)).
        Can be substituted with an HDF5 file object to minimise memory consumption.


    Notes
    -----

    - This method requires all GRIDs to be the same shape so that they can be converted into a 3-dimensional NumPy array.
    - Checks are made for normalisation
    """

    shapes = np.array([i.shape for i in grids])
    assert np.all(
        np.isclose(shapes, shapes[0])
    ), "All GRIDs must have the same dimensions"

    if weighting == "constant":
        # Use a constant weighting
        weights = np.ones(grids[0].shape[0])
    elif weighting == "power":
        # Use x**(-n)
        assert (
            "n" in weighting_kwargs
        ), "weighting_kwargs must contain `n` when weighting==power"
        power_series = np.power(
            np.arange(1, grids[0].shape[0] + 1), -weighting_kwargs["n"]
        )
        # Normalise the weights to n_grid_shells
        weights = power_series * grids[0].shape[0] / power_series.sum()
    elif callable(weighting):
        weights = weighting(grids[0].shape[0], **weighting_kwargs)

    if not isinstance(grids, np.ndarray):
        grid_input = np.array(grids)
    else:
        grid_input = grids

    # Perform cumulative summation for fast EMD calculation
    grid_cumsum = np.cumsum(grid_input, axis=-1)

    assert np.all(
        np.isclose(grid_cumsum[0, 0, -1], grid_cumsum[:, :, -1])
    ), "All GRIDs must be normalised to the same area. "

    if results_array:
        assert results_array.shape == (grid_input.shape[0], grid_input.shape[0])
        output = results_array
    else:
        output = np.zeros((grid_cumsum.shape[0], grid_cumsum.shape[0]))

    # Do the time-consuming calculation in parallel (with progress bar)
    for i in tqdm(range(grid_cumsum.shape[0]), mininterval=5):
        output[i] = _emd_cumsum_row(grid_cumsum, i, bin_width, weights=weights)
        # Fill in the lower diagonal (assumes we are iterating in order)
        output[i, :i] = output[:i, i]
    return output


def rdf_emd_similarity(rdf_a, rdf_b, max_distance=10, method="fast"):
    """
    Compute EMD similarity between two RDF distributions (1D or 2D)

    Notes
    -----

    This is the most time-consuming step in computing EMD similarity between distributions (particularly GRIDs).
    TODO: optimise this method further

    The "fast" method joins all GRID shells into a single 1D vector, and uses a modified distance metric so that
    comparisons between equivalent shells will give the same EMD, but distributions between shells have a high EMD contribution
    (**unless the distributions are not normalised**). This requires only one call to `wasserstein_distance` (with n_shell*max_dist bins)
    rather than n_shell calls to `wasserstein_distance` (each with max_dist bins).

    """

    assert rdf_a.shape == rdf_b.shape
    emd_bins = np.linspace(0, max_distance, rdf_a.shape[-1])

    if len(rdf_a.shape) == 2:
        if method == "orig":
            shell_distances = []
            for j in range(rdf_a.shape[0]):
                shell_distances.append(
                    wasserstein_distance(emd_bins, emd_bins, rdf_a[j], rdf_b[j])
                )

            return np.array(shell_distances).mean()
        elif method == "fast":
            assert np.isclose(
                rdf_a.sum(axis=1), 1.0
            ).all(), "All GRID shells must be normalized to 1.0 in order for the fast method to work correctly."
            assert np.isclose(
                rdf_b.sum(axis=1), 1.0
            ).all(), "All GRID shells must be normalized to 1.0 in order for the fast method to work correctly."
            shell_offsets = np.arange(0, rdf_a.shape[0]) * emd_bins[-1]
            emd_bins_1D = np.vstack([emd_bins] * rdf_a.shape[0]) + np.column_stack(
                [shell_offsets] * rdf_a.shape[-1]
            )
            if isinstance(rdf_a, np.ndarray) and isinstance(rdf_b, np.ndarray):
                # Using standard NumPy array processing
                emd_bins_1D = emd_bins_1D.ravel()
                return wasserstein_distance(
                    emd_bins_1D, emd_bins_1D, rdf_a.ravel(), rdf_b.ravel()
                )
            elif isinstance(rdf_a, coo_matrix) and isinstance(rdf_b, coo_matrix):
                # We are using sparse array, so can gain a significant speedup (~2-5-fold)
                emd_bins_a = emd_bins_1D[rdf_a.row, rdf_a.col]
                emd_bins_b = emd_bins_1D[rdf_b.row, rdf_b.col]
                return wasserstein_distance(
                    emd_bins_a, emd_bins_b, rdf_a.data, rdf_b.data
                )
    elif len(rdf_a.shape) == 1:
        return wasserstein_distance(emd_bins, emd_bins, rdf_a, rdf_b)


def rdf_row_similarity(baseline_rdf, all_rdf, max_distance=10):
    """
    Calculate the earth mover's distance between two RDFs
    Current support vanilla rdf and extend rdf
    Using Guassian smearing for rdf

    Args:
        all_rdf
        baseline_rdf: the rdf need to be estimated
    Return:
        a pandas dataframe of pairwise distance between baseline id and all others
        for multiple shells rdf the distance is the mean value of all shells
    """
    # used for wasserstein distance
    emd_bins = np.linspace(0, 10, 101)

    rdf_1 = baseline_rdf
    rdf_emd = pd.DataFrame(columns=["baseline"])

    if len(rdf_1.shape) == 2:
        dist_matrix = dist_matrix_1d(len(rdf_1[0]))
        for d, rdf_2 in enumerate(all_rdf):
            # rdf_len = np.array([len(rdf_1), len(rdf_2)]).min()
            # shell_distances = []
            # for j in range(rdf_len):
            #    shell_distances.append(wasserstein_distance(emd_bins, emd_bins, rdf_1[j], rdf_2[j]))
            #    #shell_distances.append(emd(rdf_1[j], rdf_2[j], dist_matrix))
            # rdf_emd.loc[d] = np.array(shell_distances).mean()
            rdf_emd.loc[d] = rdf_emd_similarity(rdf_1, rdf_2, max_distance=max_distance)

    elif len(rdf_1.shape) == 1:
        dist_matrix = dist_matrix_1d(len(rdf_1))
        for d, rdf_2 in enumerate(all_rdf):
            rdf_emd.loc[d] = wasserstein_distance(emd_bins, emd_bins, rdf_1, rdf_2)
            # rdf_emd.loc[d['task_id']] = emd(rdf_1, rdf_2, dist_matrix)

    return rdf_emd


def rdf_similarity_matrix(
    all_rdf,
    data=None,
    indice=None,
    method="emd",
    max_distance=10,
):
    """
    Calculate the earth mover's distance between all RDF pairs, returning a square matrix.

    Parameters
    ----------

    all_rdf : array-like
        Iterable of RDF representations to use for RDF calculation
    data: iterable of dicts, optional
        Iterable the same length as all_rdf, containing the key `task_id` to be used to
        label the resulting DataFrame.

    Returns
    -------

    dissimilarity : DataFrame
        DataFrame containing pairwise dissimilarity between RDF representations.

    Notes
    -----

    `rdf_similarity_matrix` has changed to use `super_fast_EMD_matrix` internally, which is better
    parallelised. More control over data storage (e.g. saving direct to a file to reduce memory) can
    be achieved by using `super_fast_EMD_matrix` directly.
    """

    warnings.warn(
        "`rdf_similarity` is now effectively parallelised using numba, so indice is no longer used.",
        SyntaxWarning,
    )

    rdf_lengths = [i.shape[0] for i in all_rdf]
    assert (
        len(set(rdf_lengths)) == 1
    ), "All RDFs (or GRIDs) must have the same number of bins - use `data_prepare.trim_rdf_bins` first"

    # Compute bin_width based on max distance
    bin_width = max_distance / rdf_lengths[0]

    if len(all_rdf[0].shape) == 2:
        # typically for extend RDF

        if data:
            ids = [i["task_id"] for i in data]
        else:
            ids = np.arange(len(all_rdf))

        dissimilarity = super_fast_EMD_matrix(
            all_rdf,
            bin_width,
            weighting="constant",
            weighting_kwargs={"n": 0.0},
            results_array=None,
        )

        df = pd.DataFrame(dissimilarity, index=ids, columns=ids)

        # df = pd.DataFrame(np.zeros((len(ids), len(ids))), index=ids, columns=ids)
        # for i1 in tqdm(range(indice[0], indice[1]), desc='similarity matrix rows', mininterval=10):
        #     d1 = data[i1]
        #     for i2, d2 in enumerate(data):
        #         if i1 <= i2:
        #             df.loc[d1['task_id'], d2['task_id']] = rdf_emd_similarity(all_rdf[i1], all_rdf[i2], max_distance=max_distance)
        #         else:
        #             df.loc[d1['task_id'], d2['task_id']] = np.nan
    elif len(all_rdf[0].shape) == 1:
        # typically for vanilla RDF and other 1D input
        df = pd.DataFrame([])
        for i1 in tqdm(
            range(indice[0], indice[1]), desc="similarity matrix rows", mininterval=10
        ):
            d1 = data[i1]
            for i2, d2 in enumerate(data):
                if i1 <= i2:
                    if method == "emd":
                        shell_distance = rdf_emd_similarity(
                            all_rdf[i1], all_rdf[i2], max_distance=max_distance
                        )

                    elif method == "cosine":
                        shell_distance = spatial.distance.cosine(
                            all_rdf[i1], all_rdf[i2]
                        )

                    df.loc[d1["task_id"], d2["task_id"]] = shell_distance
                else:
                    df.loc[d1["task_id"], d2["task_id"]] = np.nan
    return df


def rdf_similarity_matrix_old(data, all_rdf, order=None, method="emd"):
    """
    Deprecated. Have some methods there no longer used
        1. (reciprical) inner product as a similarity measure
        2. emd method implemented in pyemd
        3. the matrix in the order of lattice parameter or symmetry

    Calculate the earth mover's distance between two RDFs
    Current support vanilla rdf and extend rdf
    Using Guassian smearing for rdf

    Args:
        data: data from json
        all_rdf:
        order: how the compounds are arranged
            None: using the order in the josn 'data'
            symmetry: in the order of space group number, typically for
                influence of distortion
            lattice: in the order of lattice constant
        method: how similarity is calculated
            emd: earth mover's distance
            linear: inner product
            linear-reciprocal: reciprocal of inner product (sum), useful for visualization
                and comparison with emd results
            cosine: cosine similarity implemented in scipy
            cosine-reciprocal: reciprocal of cosine
    Return:
        a pandas dataframe with all pairwise distance
        for multiple shells rdf the distance is the mean value of all shells
    """

    warnings.warn(
        "`rdf_similarity_matrix_old` is deprecated, and will be removed in a future release.",
        DeprecationWarning,
    )

    # used for wasserstein distance
    emd_bins = np.linspace(0, 10, 101)

    if len(all_rdf[0].shape) == 2:
        # typically for extend RDF
        df = pd.DataFrame([])
        dist_matrix = dist_matrix_1d(len(all_rdf[0][0]))
        for i1, d1 in enumerate(tqdm(data, desc="rdf similarity", mininterval=60)):
            for i2, d2 in enumerate(data):
                if i1 <= i2:
                    rdf_len = np.array([len(all_rdf[i1]), len(all_rdf[i2])]).min()
                    shell_distances = []
                    for j in range(rdf_len):
                        if method == "emd":
                            # shell_distances.append(wasserstein_distance(emd_bins, emd_bins,
                            #                                            all_rdf[i1][j], all_rdf[i2][j]))
                            shell_distances.append(
                                emd(all_rdf[i1][j], all_rdf[i2][j], dist_matrix)
                            )
                        elif method == "linear" or method == "linear-reciprocal":
                            shell_distances.append(
                                np.inner(all_rdf[i1][j], all_rdf[i2][j])
                            )
                        elif method == "cosine" or method == "cosine-reciprocal":
                            shell_distances.append(
                                spatial.distance.cosine(all_rdf[i1][j], all_rdf[i2][j])
                            )

                    if method == "emd" or method == "linear" or method == "cosine":
                        df.loc[d1["task_id"], d2["task_id"]] = np.array(
                            shell_distances
                        ).mean()
                        df.loc[d2["task_id"], d1["task_id"]] = np.array(
                            shell_distances
                        ).mean()
                    elif method == "linear-reciprocal" or method == "cosine-reciprocal":
                        # add a small number 1e-11 to avoid dividing by zero
                        df.loc[d1["task_id"], d2["task_id"]] = 1.0 / (
                            np.array(shell_distances).mean() + 1e-11
                        )
                        df.loc[d2["task_id"], d1["task_id"]] = 1.0 / (
                            np.array(shell_distances).mean() + 1e-11
                        )

    elif len(all_rdf[0].shape) == 1:
        # typically for vanilla RDF and other 1D input
        df = pd.DataFrame([])
        dist_matrix = dist_matrix_1d(len(all_rdf[0]))
        for i1, d1 in enumerate(tqdm(data, desc="rdf similarity", mininterval=60)):
            for i2, d2 in enumerate(data):
                if i1 <= i2:
                    if method == "emd":
                        # see above for explanation and comparision of these two methods
                        shell_distance = wasserstein_distance(
                            emd_bins, emd_bins, all_rdf[i1], all_rdf[i2]
                        )
                        # shell_distance = emd(all_rdf[i1], all_rdf[i2], dist_matrix)
                    elif method == "linear" or method == "cosine":
                        shell_distance = np.inner(all_rdf[i1], all_rdf[i2])
                    elif method == "linear-reciprocal" or method == "cosine-reciprocal":
                        # add a small number 1e-11 to avoid dividing by zero
                        shell_distance = 1.0 / (
                            np.inner(all_rdf[i1], all_rdf[i2]) + 1e-11
                        )
                    df.loc[d1["task_id"], d2["task_id"]] = shell_distance
                    df.loc[d2["task_id"], d1["task_id"]] = shell_distance

    if order == "symmetry":
        df2 = pd.DataFrame(columns=["sg"])
        for d in data:
            struct = Structure.from_str(d["cif"], fmt="cif")
            df2.loc[d["task_id"]] = struct.get_space_group_info()[1]
        new_index = df2.sort_values("sg").index.values

        df = df.reindex(columns=new_index, index=new_index, fill_value=0)
        np.fill_diagonal(df.values, 0)

    elif order == "lattice":
        df2 = pd.DataFrame(columns=["lattice"])
        for d in data:
            struct = Structure.from_str(d["cif"], fmt="cif")
            df2.loc[d["task_id"]] = struct.lattice.a
        new_index = df2.sort_values("lattice").index.values

        df = df.reindex(columns=new_index, index=new_index, fill_value=0)
        np.fill_diagonal(df.values, 0)

    return df


def rdf_similarity_visualize(data, all_rdf, mode, base_id=31):
    """
    Visualization of rdf similarity results.
    Currently only used to investigate the influence of lattice constant

    Args:
        data: data from json
        all_rdf:
        base_id: ID number for the baseline structure, i.e. all others structures are compared
            to this structure
    Return:
        a pandas dataframe with all pairwise distance
        for multiple shells rdf the distance is the mean value of all shells
    """

    df = pd.DataFrame([])
    if mode == "similarity_different_shell":
        vis_rdf_shells = [0, 2, 6, 12, 14, 20, 26, 28]
        for i2, d2 in enumerate(data):
            for j in vis_rdf_shells:
                df.loc[d2["task_id"], str(j)] = wasserstein_distance(
                    all_rdf[base_id][j], all_rdf[i2][j]
                )

    elif mode == "similarity_different_":
        dist_list = []
        mp_indice = []
        vis_ids = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]
        for i2 in vis_ids:
            mp_indice.append(data[i2]["task_id"])

            rdf_len = np.array([len(all_rdf[base_id]), len(all_rdf[i2])]).min()
            shell_distances = []
            for j in range(rdf_len):
                shell_distances.append(
                    wasserstein_distance(all_rdf[base_id][j], all_rdf[i2][j])
                )
            dist_list.append(shell_distances)

        df = pd.DataFrame(dist_list, index=mp_indice).transpose()

    elif mode == "rdf_shell":
        rdf0s = []
        local_extrems = [21, 25, 29, 31, 36, 40]
        for i in local_extrems:
            rdf0s.append(all_rdf[i][0])
        df = pd.DataFrame(rdf0s, index=local_extrems).transpose()

    elif mode == "rdf_shell_emd_path":
        emd_flows = []
        local_extrems = [21, 25, 29, 36, 40]
        dist_matrix = dist_matrix_1d(len(all_rdf[0][0]))

        for i in local_extrems:
            em = emd_with_flow(all_rdf[base_id][0], all_rdf[i][0], dist_matrix)
            print(em[0], wasserstein_distance(all_rdf[base_id][0], all_rdf[i][0]))
            emd_flows.append(em[1])
        df = pd.DataFrame(emd_flows, index=local_extrems)

    elif mode == "lattice_difference":
        nums = [0, 259, 544]
        for base_id in nums:
            a1 = Structure.from_str(data[base_id]["cif"], fmt="cif").lattice.a
            for i2, d in enumerate(data):
                a2 = Structure.from_str(data[i2]["cif"], fmt="cif").lattice.a
                rdf_len = np.array([len(all_rdf[base_id]), len(all_rdf[i2])]).min()
                shell_distances = []
                for j in range(rdf_len):
                    shell_distances.append(
                        wasserstein_distance(all_rdf[base_id][j], all_rdf[i2][j])
                    )
                df.loc[data[i2]["task_id"], data[base_id]["task_id"] + "lattice"] = (
                    a1 - a2
                )
                df.loc[data[i2]["task_id"], data[base_id]["task_id"]] = np.array(
                    shell_distances
                ).mean()

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Earth Mover Distance",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--input_file",
        type=str,
        default="../MP_modulus.json",
        help="the bulk modulus and structure from Materials Project",
    )
    parser.add_argument(
        "--output_file", type=str, default="", help="currently for rdf similarity"
    )
    parser.add_argument(
        "--rdf_dir", type=str, default="./", help="dir has all the rdf files"
    )
    parser.add_argument(
        "--simi_dir", type=str, default="./", help="dir containing distance matrices"
    )
    parser.add_argument(
        "--task",
        type=str,
        default="rdf_similarity",
        help="which property to be calculated: \n"
        + "   rdf_similarity: \n"
        + "   rdf_similarity_matrix: \n"
        + "   composition_similarity_matrix: \n"
        + "   composition_similarity: \n"
        + "   rdf_similarity_visualize: \n"
        + "   find_same_structure: find all structures giving same rdf \n"
        + "   find_same_rdf: find all same rdf \n"
        + "   nn_bulk_modulus: predict bulk modulus using nearest neighbor \n"
        + "   ",
    )
    parser.add_argument(
        "--baseline_id",
        type=str,
        default=None,
        help="only used for single rdf_similarity composition_similarity tasks",
    )
    parser.add_argument(
        "--data_indice",
        type=str,
        default=None,
        help="start and end indice of the sub dataset",
    )
    parser.add_argument(
        "-p",
        "--procs",
        type=int,
        default=None,
        help="Number of processors to parallelize over where implemented",
    )

    args = parser.parse_args()
    input_file = args.input_file
    output_file = args.output_file
    rdf_dir = args.rdf_dir
    task = args.task
    baseline_id = args.baseline_id
    procs = args.procs

    if isinstance(args.data_indice, str):
        indice = list(map(int, args.data_indice.split("_")))
    else:
        indice = None

    with open(input_file, "r") as f:
        data = json.load(f)

    if task == "rdf_similarity":
        if procs is not None and procs > 1:
            all_rdf = rdf_read_parallel(data, rdf_dir, procs=procs)
        else:
            all_rdf = rdf_read(data, rdf_dir)
        baseline_rdf = np.loadtxt(rdf_dir + "/" + baseline_id, delimiter=" ")
        rdf_emd = rdf_row_similarity(baseline_rdf, all_rdf)
        rdf_emd.to_csv(output_file + baseline_id + "_rdf_emd.csv")

    elif task == "rdf_similarity_matrix":
        if procs is not None and procs > 1:
            all_rdf = rdf_read_parallel(data, rdf_dir, procs=procs)
        else:
            all_rdf = rdf_read(data, rdf_dir)

        df = rdf_similarity_matrix(all_rdf, data, indice=indice, method="emd")
        df.to_csv(output_file + "_whole_matrix.csv")

    elif task == "composition_similarity":
        elem_vectors, elem_symbols = composition_one_hot(data)
        compo_emd = composition_similarity(baseline_id, elem_vectors)
        compo_emd.to_csv(output_file + baseline_id + "_compos_emd.csv")

    elif task == "composition_similarity_matrix":
        elem_vectors, elem_symbols = composition_one_hot(data)
        compo_emd = composition_similarity_matrix(elem_vectors, indice=indice)
        if indice:
            compo_emd.to_csv(
                output_file + str(indice[0]) + "_" + str(indice[1]) + ".csv"
            )
        else:
            compo_emd.to_csv(output_file + "_whole_matrix.csv")

    elif task == "rdf_similarity_visualize":
        if procs is not None and procs > 1:
            all_rdf = rdf_read_parallel(data, rdf_dir, procs=procs)
        else:
            all_rdf = rdf_read(data, rdf_dir)

        for mode in ["rdf_shell_emd_path"]:
            df = rdf_similarity_visualize(data, all_rdf, mode=mode)
            df.to_csv(output_file + "_" + mode + ".csv")

    elif task == "find_same_structure":
        match_list = find_same_structure(data)
        with open(output_file, "w") as f:
            json.dump(match_list, f, indent=1)

    elif task == "find_same_rdf":
        if procs is not None and procs > 1:
            all_rdf = rdf_read_parallel(data, rdf_dir, procs=procs)
        else:
            all_rdf = rdf_read(data, rdf_dir)
        all_rdf = rdf_trim(all_rdf)
        X_data = rdf_flatten(all_rdf)
        match_list = find_same_rdf(all_rdf, data)
        with open(output_file, "w") as f:
            json.dump(match_list, f, indent=1)

    elif task == "nn_bulk_modulus":
        if baseline_id:
            # for single point calculation
            mp_ids = os.listdir(os.path.join(sys.path[0], "../rdf_emd/"))
            for f in mp_ids:
                print(nn_bulk_modulus_single(f.replace("_emd.csv", ""), data))
        else:
            pred_k = nn_bulk_modulus_matrix_add(
                data, simi_matrix=["extended_rdf_emd", "composition_emd"]
            )
            pred_k.to_csv(output_file + "pred_k.csv")
