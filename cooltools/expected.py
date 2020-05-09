from itertools import chain, combinations
from collections import defaultdict
from functools import partial

import warnings

import numpy as np
import pandas as pd
from scipy.linalg import toeplitz
from scipy.signal import fftconvolve

from cooler.tools import split, partition
import cooler
import bioframe
from .lib import assign_supports, numutils

where = np.flatnonzero
concat = chain.from_iterable


def _contact_areas(distbins, scaffold_length):
    distbins = distbins.astype(float)
    scaffold_length = float(scaffold_length)
    outer_areas = np.maximum(scaffold_length - distbins[:-1], 0) ** 2
    inner_areas = np.maximum(scaffold_length - distbins[1:], 0) ** 2
    return 0.5 * (outer_areas - inner_areas)


def contact_areas(distbins, region1, region2):
    if region1 == region2:
        start, end = region1
        areas = _contact_areas(distbins, end - start)
    else:
        start1, end1 = region1
        start2, end2 = region2
        if start2 <= start1:
            start1, start2 = start2, start1
            end1, end2 = end2, end1
        areas = (
            _contact_areas(distbins, end2 - start1)
            - _contact_areas(distbins, start2 - start1)
            - _contact_areas(distbins, end2 - end1)
        )
        if end1 < start2:
            areas += _contact_areas(distbins, start2 - end1)

    return areas


def compute_scaling(df, region1, region2=None, dmin=int(1e1), dmax=int(1e7), n_bins=50):

    import dask.array as da

    if region2 is None:
        region2 = region1

    distbins = numutils.logbins(dmin, dmax, N=n_bins)
    areas = contact_areas(distbins, region1, region2)

    df = df[
        (df["pos1"] >= region1[0])
        & (df["pos1"] < region1[1])
        & (df["pos2"] >= region2[0])
        & (df["pos2"] < region2[1])
    ]
    dists = (df["pos2"] - df["pos1"]).values

    if isinstance(dists, da.Array):
        obs, _ = da.histogram(dists[(dists >= dmin) & (dists < dmax)], bins=distbins)
    else:
        obs, _ = np.histogram(dists[(dists >= dmin) & (dists < dmax)], bins=distbins)

    return distbins, obs, areas



def lattice_pdist_frequencies(n, points):
    """
    Distribution of pairwise 1D distances among a collection of distinct
    integers ranging from 0 to n-1.

    Parameters
    ----------
    n : int
        Size of the lattice on which the integer points reside.
    points : sequence of int
        Arbitrary integers between 0 and n-1, inclusive, in any order but
        with no duplicates.

    Returns
    -------
    h : 1D array of length n
        h[d] counts the number of integer pairs that are exactly d units apart

    Notes
    -----
    This is done using a convolution via FFT. Thanks to Peter de Rivaz; see
    `<http://stackoverflow.com/questions/42423823/distribution-of-pairwise-distances-between-many-integers>`_.

    """
    if len(np.unique(points)) != len(points):
        raise ValueError("Integers must be distinct.")
    x = np.zeros(n)
    x[points] = 1
    return np.round(fftconvolve(x, x[::-1], mode="full")).astype(int)[-n:]


def count_bad_pixels_per_diag(n, bad_bins):
    """
    Efficiently count the number of bad pixels on each upper diagonal of a
    matrix assuming a sequence of bad bins forms a "grid" of invalid pixels.

    Each bad bin bifurcates into two a row and column of bad pixels, so an
    upper bound on number of bad pixels per diagonal is 2*k, where k is the
    number of bad bins. For a given diagonal, we need to subtract from this
    upper estimate the contribution from rows/columns reaching "out-of-bounds"
    and the contribution of the intersection points of bad rows with bad
    columns that get double counted.

    ::

        o : bad bin
        * : bad pixel
        x : intersection bad pixel
        $ : out of bounds bad pixel
             $    $     $
         *--------------------------+
          *  *    *     *           |
           * *    *     *           |
            **    *     *           |
             o****x*****x***********|$
              *   *     *           |
               *  *     *           |
                * *     *           |
                 o******x***********|$
                  *     *           |
                   *    *           |
                    *   *           |
                     *  *           |
                      * *           |
                       **           |
                        o***********|$
                         *          |
                          *         |

    Parameters
    ----------
    n : int
        total number of bins
    bad_bins : 1D array of int
        sorted array of bad bin indexes

    Returns
    -------
    dcount : 1D array of length n
        dcount[d] == number of bad pixels on diagonal d

    """
    k = len(bad_bins)
    dcount = np.zeros(n, dtype=int)

    # Store all intersection pixels in a separate array
    # ~O(n log n) with fft
    ixn = lattice_pdist_frequencies(n, bad_bins)
    dcount[0] = ixn[0]

    # Keep track of out-of-bounds pixels by squeezing left and right bounds
    # ~O(n)
    pl = 0
    pr = k
    for diag in range(1, n):
        if pl < k:
            while (bad_bins[pl] - diag) < 0:
                pl += 1
                if pl == k:
                    break
        if pr > 0:
            while (bad_bins[pr - 1] + diag) >= n:
                pr -= 1
                if pr == 0:
                    break
        dcount[diag] = 2 * k - ixn[diag] - pl - (k - pr)
    return dcount


def count_all_pixels_per_diag(n):
    """
    Total number of pixels on each upper diagonal of a square matrix.

    Parameters
    ----------
    n : int
        total number of bins (dimension of square matrix)

    Returns
    -------
    dcount : 1D array of length n
        dcount[d] == total number of pixels on diagonal d

    """
    return np.arange(n, 0, -1)


def count_all_pixels_per_block(clr, supports):
    """
    Calculate total number of pixels per rectangular block of a contact map
    defined as a paired-combination of genomic "support" regions.

    Parameters
    ----------
    clr : cooler.Cooler
        Input cooler
    supports : list
        list of genomic support regions

    Returns
    -------
    blocks : dict
        dictionary with total number of pixels per pair of support regions
    """
    n = len(supports)
    x = [clr.extent(region)[1] - clr.extent(region)[0]
         for region in supports]
    blocks = {}
    for i in range(n):
        for j in range(i + 1, n):
            blocks[supports[i], supports[j]] = x[i] * x[j]
    return blocks



def count_bad_pixels_per_block(clr, supports, weight_name="weight", bad_bins=None):
    """
    Calculate number of "bad" pixels per rectangular block of a contact map
    defined as a paired-combination of genomic "support" regions.

    "Bad" pixels are inferred from the balancing weight column `weight_name` or
    provided directly in the form of an array `bad_bins`.

    Setting `weight_name` and `bad_bins` to `None` yields 0 bad pixels per
    combination of support regions.

    Parameters
    ----------
    clr : cooler.Cooler
        Input cooler
    supports : list
        a list of genomic support regions
    weight_name : str
        name of the weight vector in the "bins" table,
        if weight_name is None returns 0 for each block.
        Balancing weight are used to infer bad bins.
    bad_bins : array-like
        a list of bins to ignore per support region.
        Overwrites inference of bad bins from balacning
        weight [to be implemented].

    Returns
    -------
    blocks : dict
        dictionary with the number of "bad" pixels per pair of support regions
    """
    n = len(supports)

    if bad_bins is not None:
        raise NotImplementedError("providing external list \
            of bad bins is not implemented.")

    # Get the total number of bins per region
    n_tot = []
    for region in supports:
        lo, hi = clr.extent(region)
        n_tot.append(hi - lo)

    # Get the number of bad bins per region
    if weight_name is None:
        # ignore bad bins
        # useful for unbalanced data
        n_bad = [0 for region in supports]
    elif isinstance(weight_name, str):
        if weight_name not in clr.bins().columns:
            raise KeyError("Balancing weight {weight_name} not found!")
        # bad bins are ones with the weight vector being NaN
        n_bad = [
            np.sum(
                clr
                .bins()[weight_name]
                .fetch(region)
                .isnull()
                .astype(int)
                .values
            )
            for region in supports
        ]
    else:
        raise ValueError("`weight_name` can be `str` or `None`")

    # Calculate the resulting bad pixels in trans
    blocks = {}
    for i in range(n):
        for j in range(i + 1, n):
            blocks[supports[i], supports[j]] = (
                n_tot[i] * n_bad[j] +
                n_tot[j] * n_bad[i] -
                n_bad[i] * n_bad[j]
            )
    return blocks



def make_diag_table(bad_mask, span1, span2):
    """
    Compute the total number of elements ``n_elem`` and the number of bad
    elements ``n_bad`` per diagonal for a single contact area encompassing
    ``span1`` and ``span2`` on the same genomic scaffold (cis matrix).

    Follows the same principle as the algorithm for finding contact areas for
    computing scalings.

    Parameters
    ----------
    bad_mask : 1D array of bool
        Mask of bad bins for the whole genomic scaffold containing the regions
        of interest.
    span1, span2 : pair of ints
        The bin spans (not genomic coordinates) of the two regions of interest.

    Returns
    -------
    diags : pandas.DataFrame
        Table indexed by 'diag' with columns ['n_elem', 'n_bad'].

    """

    def _make_diag_table(n_bins, bad_locs):
        diags = pd.DataFrame(index=pd.Series(np.arange(n_bins), name="diag"))
        diags["n_elem"] = count_all_pixels_per_diag(n_bins)
        diags["n_valid"] = diags["n_elem"] - count_bad_pixels_per_diag(n_bins, bad_locs)
        return diags

    if span1 == span2:
        lo, hi = span1
        diags = _make_diag_table(hi - lo, where(bad_mask[lo:hi]))
    else:
        lo1, hi1 = span1
        lo2, hi2 = span2
        if lo2 <= lo1:
            lo1, lo2 = lo2, lo1
            hi1, hi2 = hi2, hi1
        diags = (
            _make_diag_table(hi2 - lo1, where(bad_mask[lo1:hi2]))
            .subtract(
                _make_diag_table(lo2 - lo1, where(bad_mask[lo1:lo2])), fill_value=0
            )
            .subtract(
                _make_diag_table(hi2 - hi1, where(bad_mask[hi1:hi2])), fill_value=0
            )
        )
        if hi1 < lo2:
            diags.add(
                _make_diag_table(lo2 - hi1, where(bad_mask[hi1:lo2])), fill_value=0
            )
        diags = diags[diags["n_elem"] > 0]

    diags = diags.drop("n_elem", axis=1)
    return diags.astype(int)


def _sum_diagonals(df, field):
    reduced = df.groupby("diag")[field].sum()
    reduced.name = field + ".sum"
    return reduced


def make_diag_tables(clr, supports, weight_name="weight", bad_bins=None):
    """
    For every support region infer diagonals that intersect this region
    and calculate the size of these intersections in pixels, both "total" and
    "n_valid", where "n_valid" does not include "bad" bins into counting.

    "Bad" pixels are inferred from the balancing weight column `weight_name` or
    provided directly in the form of an array `bad_bins`.

    Setting `weight_name` and `bad_bins` to `None` yields 0 "bad" pixels per
    diagonal per support region.

    Parameters
    ----------
    clr : cooler.Cooler
        Input cooler
    supports : list
        a list of genomic support regions
    weight_name : str
        name of the weight vector in the "bins" table,
        if weight_name is None returns 0 for each block.
        Balancing weight are used to infer bad bins.
    bad_bins : array-like
        a list of bins to ignore. Indexes of bins must
        be absolute, as in clr.bins()[:], as opposed to
        being offset by chromosome start.
        "bad_bins" will be combined with the bad bins
        masked by balancing if there are any.

    Returns
    -------
    diag_tables : dict
        dictionary with DataFrames of relevant diagonals for every support.
    """

    bins = clr.bins()[:]
    if weight_name is None:
        # ignore bad bins
        sizes = dict(bins.groupby("chrom").size())
        bad_bin_dict = {
            chrom: np.zeros(sizes[chrom], dtype=bool) for chrom in sizes.keys()
        }
    elif isinstance(weight_name, str):
        # using balacning weight to infer bad bins
        if weight_name not in clr.bins().columns:
            raise KeyError("Balancing weight {weight_name} not found!")
        groups = dict(iter(bins.groupby("chrom")[weight_name]))
        bad_bin_dict = {
            chrom: np.array(groups[chrom].isnull()) for chrom in groups.keys()
        }
    else:
        raise ValueError("`weight_name` can be `str` or `None`")

    # combine custom "bad_bins" with "bad_bin_dict":
    if bad_bins is not None:
        # check if "bad_bins" are legit:
        try:
            bad_bins_chrom = bins.iloc[bad_bins].reset_index(drop=False)
        except IndexError:
            raise ValueError("Provided `bad_bins` are incorrect or out-of-bound")
        # group them by observed chromosomes only
        bad_bins_grp = bad_bins_chrom[["index","chrom"]].groupby("chrom",observed=True)
        # update "bad_bin_dict" with "bad_bins" for each chrom:
        for chrom, bin_ids in bad_bins_grp["index"]:
            co = clr.offset(chrom)
            # adjust by chromosome offset
            bad_bin_dict[chrom][bin_ids.values - co] = True


    where = np.flatnonzero
    diag_tables = {}
    for region in supports:
        # parse region if str
        if isinstance(region, str):
            region = bioframe.parse_region(region)
        # unpack region(s) into chroms,starts,ends
        if len(region) == 1:
            chrom, = region
            start1, end1 = 0, clr.chromsizes[chrom]
            start2, end2 = start1, end1
        elif len(region) == 2:
            chrom, start1, end1 = region[0]
            _, start2, end2 = region[1]
        elif len(region) == 3:
            chrom, start1, end1 = region
            start2, end2 = start1, end1
        elif len(region) == 5:
            chrom, start1, end1, start2, end2 = region
        else:
            raise ValueError("Regions must be sequences of length 1, 3 or 5")

        # translate regions into relative bin id-s:
        lo1, hi1 = clr.extent((chrom, start1, end1))
        lo2, hi2 = clr.extent((chrom, start2, end2))
        co = clr.offset(chrom)
        lo1 -= co
        lo2 -= co
        hi1 -= co
        hi2 -= co

        bad_mask = bad_bin_dict[chrom]
        diag_tables[region] = make_diag_table(bad_mask, [lo1, hi1], [lo2, hi2])

    return diag_tables


def _diagsum_symm(clr, fields, transforms, supports, span):
    lo, hi = span
    bins = clr.bins()[:]
    pixels = clr.pixels()[lo:hi]
    pixels = cooler.annotate(pixels, bins, replace=False)

    pixels["support1"] = assign_supports(pixels, supports, suffix="1")
    pixels["support2"] = assign_supports(pixels, supports, suffix="2")
    pixels = pixels[pixels["support1"] == pixels["support2"]].copy()

    pixels["diag"] = pixels["bin2_id"] - pixels["bin1_id"]
    for field, t in transforms.items():
        pixels[field] = t(pixels)

    pixelgroups = dict(iter(pixels.groupby("support1")))
    return {
        int(i): group.groupby("diag")[fields].sum() for i, group in pixelgroups.items()
    }


def _diagsum_asymm(clr, fields, transforms, contact_type, supports1, supports2, span):
    lo, hi = span
    bins = clr.bins()[:]
    pixels = clr.pixels()[lo:hi]
    pixels = cooler.annotate(pixels, bins, replace=False)

    if contact_type == "cis":
        pixels = pixels[pixels["chrom1"] == pixels["chrom2"]].copy()
    elif contact_type == "trans":
        pixels = pixels[pixels["chrom1"] != pixels["chrom2"]].copy()

    pixels["diag"] = pixels["bin2_id"] - pixels["bin1_id"]
    for field, t in transforms.items():
        pixels[field] = t(pixels)

    pixels["support1"] = assign_supports(pixels, supports1, suffix="1")
    pixels["support2"] = assign_supports(pixels, supports2, suffix="2")

    pixel_groups = dict(iter(pixels.groupby(["support1", "support2"])))
    return {
        (int(i), int(j)): group.groupby("diag")[fields].sum()
        for (i, j), group in pixel_groups.items()
    }


def _blocksum_asymm(clr, fields, transforms, supports1, supports2, span):
    lo, hi = span
    bins = clr.bins()[:]
    pixels = clr.pixels()[lo:hi]
    pixels = cooler.annotate(pixels, bins, replace=False)

    pixels = pixels[pixels["chrom1"] != pixels["chrom2"]].copy()
    for field, t in transforms.items():
        pixels[field] = t(pixels)

    pixels["support1"] = assign_supports(pixels, supports1, suffix="1")
    pixels["support2"] = assign_supports(pixels, supports2, suffix="2")
    pixels = pixels.dropna()

    pixel_groups = dict(iter(pixels.groupby(["support1", "support2"])))
    return {
        (int(i), int(j)): group[fields].sum() for (i, j), group in pixel_groups.items()
    }


def diagsum(
    clr,
    supports,
    transforms=None,
    weight_name="weight",
    bad_bins=None,
    chunksize=10000000,
    ignore_diags=2,
    map=map
):
    """

    Intra-chromosomal diagonal summary statistics.

    Parameters
    ----------
    clr : cooler.Cooler
        Cooler object
    supports : sequence of genomic range tuples
        Support regions for intra-chromosomal diagonal summation
    transforms : dict of str -> callable, optional
        Transformations to apply to pixels. The result will be assigned to
        a temporary column with the name given by the key. Callables take
        one argument: the current chunk of the (annotated) pixel dataframe.
    weight_name : str
        name of the balancing weight vector used to count
        "bad"(masked) pixels per diagonal.
        Use `None` to avoid masking "bad" pixels.
    bad_bins : array-like
        a list of bins to ignore per support region.
        Overwrites inference of bad bins from balacning
        weight [to be implemented].
    chunksize : int, optional
        Size of pixel table chunks to process
    ignore_diags : int, optional
        Number of intial diagonals to exclude from statistics
    map : callable, optional
        Map functor implementation.

    Returns
    -------
    dict of support region -> dataframe of diagonal statistics

    """
    spans = partition(0, len(clr.pixels()), chunksize)
    fields = ["count"] + list(transforms.keys())
    dtables = make_diag_tables(clr, supports, weight_name=weight_name, bad_bins=bad_bins)

    for dt in dtables.values():
        for field in fields:
            agg_name = "{}.sum".format(field)
            dt[agg_name] = 0

    job = partial(_diagsum_symm, clr, fields, transforms, supports)
    results = map(job, spans)
    for result in results:
        for i, agg in result.items():
            support = supports[i]
            for field in fields:
                agg_name = "{}.sum".format(field)
                dtables[support][agg_name] = dtables[support][agg_name].add(
                    agg[field], fill_value=0
                )

    if ignore_diags:
        for dt in dtables.values():
            for field in fields:
                agg_name = "{}.sum".format(field)
                j = dt.columns.get_loc(agg_name)
                dt.iloc[:ignore_diags, j] = np.nan

    return dtables


def diagsum_asymm(
    clr,
    supports1,
    supports2,
    contact_type="cis",
    transforms=None,
    weight_name="weight",
    bad_bins=None,
    chunksize=10000000,
    ignore_diags=2,
    map=map
):
    """

    Intra-chromosomal diagonal summary statistics.

    Parameters
    ----------
    clr : cooler.Cooler
        Cooler object
    supports : sequence of genomic range tuples
        Support regions for intra-chromosomal diagonal summation
    transforms : dict of str -> callable, optional
        Transformations to apply to pixels. The result will be assigned to
        a temporary column with the name given by the key. Callables take
        one argument: the current chunk of the (annotated) pixel dataframe.
    weight_name : str
        name of the balancing weight vector used to count
        "bad"(masked) pixels per diagonal.
        Use `None` to avoid masking "bad" pixels.
    bad_bins : array-like
        a list of bins to ignore per support region.
        Overwrites inference of bad bins from balacning
        weight [to be implemented].
    chunksize : int, optional
        Size of pixel table chunks to process
    ignore_diags : int, optional
        Number of intial diagonals to exclude from statistics
    map : callable, optional
        Map functor implementation.

    Returns
    -------
    dict of support region -> dataframe of diagonal statistics

    """
    spans = partition(0, len(clr.pixels()), chunksize)
    fields = ["count"] + list(transforms.keys())
    areas = list(zip(supports1, supports2))
    dtables = make_diag_tables(clr, areas, weight_name=weight_name, bad_bins=bad_bins)

    for dt in dtables.values():
        for field in fields:
            agg_name = "{}.sum".format(field)
            dt[agg_name] = 0

    job = partial(
        _diagsum_asymm, clr, fields, transforms, contact_type, supports1, supports2
    )
    results = map(job, spans)
    for result in results:
        for (i, j), agg in result.items():
            support1 = supports1[i]
            support2 = supports2[j]
            for field in fields:
                agg_name = "{}.sum".format(field)
                dtables[support1, support2][agg_name] = dtables[support1, support2][
                    agg_name
                ].add(agg[field], fill_value=0)

    if ignore_diags:
        for dt in dtables.values():
            for field in fields:
                agg_name = "{}.sum".format(field)
                j = dt.columns.get_loc(agg_name)
                dt.iloc[:ignore_diags, j] = np.nan

    return dtables


def blocksum_pairwise(
    clr,
    supports,
    transforms=None,
    weight_name="weight",
    bad_bins=None,
    chunksize=1000000,
    map=map
):
    """
    Summary statistics on inter-chromosomal rectangular blocks.

    Parameters
    ----------
    clr : cooler.Cooler
        Cooler object
    supports : sequence of genomic range tuples
        Support regions for summation. Blocks for all pairs of support regions
        will be used.
    transforms : dict of str -> callable, optional
        Transformations to apply to pixels. The result will be assigned to
        a temporary column with the name given by the key. Callables take
        one argument: the current chunk of the (annotated) pixel dataframe.
    weight_name : str
        name of the balancing weight vector used to count
        "bad"(masked) pixels per block.
        Use `None` to avoid masking "bad" pixels.
    bad_bins : array-like
        a list of bins to ignore per support region.
        Overwrites inference of bad bins from balacning
        weight [to be implemented].
    chunksize : int, optional
        Size of pixel table chunks to process
    map : callable, optional
        Map functor implementation.

    Returns
    -------
    dict of support region -> (field name -> summary)

    """

    blocks = list(combinations(supports, 2))
    supports1, supports2 = list(zip(*blocks))
    spans = partition(0, len(clr.pixels()), chunksize)
    fields = ["count"] + list(transforms.keys())

    n_tot = count_all_pixels_per_block(clr, supports)
    n_bad = count_bad_pixels_per_block(clr, supports, weight_name=weight_name, bad_bins=bad_bins)
    records = {(c1, c2): defaultdict(int) for (c1, c2) in blocks}
    for c1, c2 in blocks:
        records[c1, c2]["n_valid"] = n_tot[c1, c2] - n_bad[c1, c2]

    job = partial(_blocksum_asymm, clr, fields, transforms, supports1, supports2)
    results = map(job, spans)
    for result in results:
        for (i, j), agg in result.items():
            for field in fields:
                agg_name = "{}.sum".format(field)
                s = agg[field].item()
                if not np.isnan(s):
                    records[supports1[i], supports2[j]][agg_name] += s

    return records
