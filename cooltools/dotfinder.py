"""
Collection of functions needed for dot-calling

"""
from functools import partial, reduce
import multiprocess as mp

from scipy.linalg import toeplitz
from scipy.ndimage import convolve
from scipy.stats import poisson
from scipy.sparse import coo_matrix
import numpy as np
import pandas as pd
from sklearn.cluster import Birch
import cooler

from .lib.numutils import LazyToeplitz, get_kernel


# these are the constants from HiCCUPS, that dictate how initial histogramms
# for lambda-chunking are computed: W1 is the # of logspaced lambda bins,
# and W2 is maximum "allowed" raw number of contacts per Hi-C heatmap bin:
HiCCUPS_W1_MAX_INDX = 40

# HFF combined exceeded this limit ...
HiCCUPS_W1_MAX_INDX = 46

# we are not using 'W2' as we're building
# the histograms dynamically:
HiCCUPS_W2_MAX_INDX = 10000


def get_qvals(pvals):
    '''
    B&H FDR control procedure: sort a given array of N p-values, determine their
    rank i and then for each p-value compute a corres- ponding q-value, which is
    a minimum FDR at which that given p-value would pass a BH-FDR test.

    Parameters
    ----------
    pvals : array-like
        array of p-values to use for multiple hypothesis testing

    Returns
    -------
    qvals : numpy.ndarray
        array of q-values

    Notes
    -----
    - BH-FDR reminder: given an array of N p-values, sort it in ascending order
    p[1]<p[2]<p[3]<...<p[N], and find a threshold p-value, p[j] for which
    p[j] < FDR*j/N, and p[j+1] is already p[j+1] >= FDR*(j+1)/N. Peaks
    corresponding to p-values p[1]<p[2]<...p[j] are considered significant.

    - Mostly follows the statsmodels implementation:
    http://www.statsmodels.org/dev/_modules/statsmodels/stats/multitest.html

    - Using alpha=0.02 it is possible to achieve called dots similar to
    pre-update status alpha is meant to be a q-value threshold: "qvals <= alpha"

    '''
    # NOTE: This is tested and is capable of reproducing
    # `multiple_test_BH` results
    pvals = np.asarray(pvals)
    n_obs = pvals.size
    # determine rank of p-values (1-indexed):
    porder = np.argsort(pvals)
    prank  = np.empty_like(porder)
    prank[porder] = np.arange(1, n_obs+1)
    # q-value = p-value*N_obs/i(rank of a p-value) ...
    qvals = np.true_divide(n_obs*pvals, prank)
    # return the qvals sorted as the initial pvals:
    return qvals


def clust_2D_pixels(pixels_df,
                    threshold_cluster=2,
                    bin1_id_name='bin1_id',
                    bin2_id_name='bin2_id',
                    clust_label_name='c_label',
                    clust_size_name='c_size',
                    verbose=True):
    '''
    Group significant pixels by proximity using Birch clustering. We use
    "n_clusters=None", which implies no AgglomerativeClustering, and thus
    simply reporting "blobs" of pixels of radii <="threshold_cluster" along
    with corresponding blob-centroids as well.

    Parameters
    ----------
    pixels_df : pandas.DataFrame
        a DataFrame with pixel coordinates that must have at least 2 columns
        named 'bin1_id' and 'bin2_id', where first is pixels's row and the
        second is pixel's column index.
    threshold_cluster : int
        clustering radius for Birch clustering derived from ~40kb radius of
        clustering and bin size.
    bin1_id_name : str
        Name of the 1st coordinate (row index) in 'pixel_df', by default
        'bin1_id'. 'start1/end1' could be usefull as well.
    bin2_id_name : str
        Name of the 2nd coordinate (column index) in 'pixel_df', by default
        'bin2_id'. 'start2/end2' could be usefull as well.
    clust_label_name : str
        Name of the cluster of pixels label. "c_label" by default.
    clust_size_name : str
        Name of the cluster of pixels size. "c_size" by default.
    verbose : bool
        Print verbose clustering summary report defaults is True.

    Returns
    -------
    peak_tmp : pandas.DataFrame
        DataFrame with the following columns:
        [c+bin1_id_name, c+bin2_id_name, clust_label_name, clust_size_name]
        row/col (bin1/bin2) are coordinates of centroids,
        label and sizes are unique pixel-cluster
        labels and their corresponding sizes.
    '''

    # col (bin2) must precede row (bin1):
    pixels     = pixels_df[[bin1_id_name, bin2_id_name]].values.astype(np.float)
    # added astype(float) to avoid further issues with clustering, as it
    # turned out start1/start2 genome coordinates could be int32 or int64
    # and int32 is not enough for some operations, i.e., integer overflow.
    pixel_idxs = pixels_df.index

    # perform BIRCH clustering of pixels:
    # "n_clusters=None" implies using BIRCH without AgglomerativeClustering,
    # thus simply reporting "blobs" of pixels of radius "threshold_cluster"
    # along with blob-centroids as well:
    brc = Birch(n_clusters=None,
                threshold=threshold_cluster,
                # branching_factor=50, (it's default)
                compute_labels=True)
    brc.fit(pixels)
    # # following is redundant,
    # # as it's done here:
    # # https://github.com/scikit-learn/scikit-learn/blob/a24c8b464d094d2c468a16ea9f8bf8d42d949f84/sklearn/cluster/birch.py#L638
    # clustered_labels = brc.predict(pixels)

    # labels of nearest centroid, assigned to each pixel,
    # BEWARE: labels might not be continuous, i.e.,
    # "np.unique(clustered_labels)" isn't same as "brc.subcluster_labels_", because:
    # https://github.com/scikit-learn/scikit-learn/blob/a24c8b464d094d2c468a16ea9f8bf8d42d949f84/sklearn/cluster/birch.py#L576
    clustered_labels    = brc.labels_
    # centroid coordinates ( <= len(clustered_labels)):
    clustered_centroids = brc.subcluster_centers_
    # count unique labels and get their continuous indices
    uniq_labels, inverse_idx, uniq_counts = np.unique(
                                                clustered_labels,
                                                return_inverse=True,
                                                return_counts=True)
    # cluster sizes taken to match labels:
    cluster_sizes       = uniq_counts[inverse_idx]
    # take centroids corresponding to labels (as many as needed):
    centroids_per_pixel = np.take(clustered_centroids,
                                  clustered_labels,
                                  axis=0)

    if verbose:
        # prepare clustering summary report:
        msg = "Clustering is completed:\n" + \
              "{} clusters detected\n".format(uniq_counts.size) + \
              "{:.2f}+/-{:.2f} mean size\n".format(uniq_counts.mean(),
                                                 uniq_counts.std())
        print(msg)

    # create output DataFrame
    centroids_n_labels_df = pd.DataFrame(
                                centroids_per_pixel,
                                index=pixel_idxs,
                                columns=['c'+bin1_id_name,'c'+bin2_id_name])
    # add labels per pixel:
    centroids_n_labels_df[clust_label_name] = clustered_labels.astype(np.int)
    # add cluster sizes:
    centroids_n_labels_df[clust_size_name] = cluster_sizes.astype(np.int)


    return centroids_n_labels_df


def diagonal_matrix_tiling(start, stop, bandwidth, edge=0, verbose=False):
    """
    Generate a stream of tiling coordinates that guarantee to cover a diagonal
    area of the matrix of size 'bandwidth'. cis-signal only!

    Parameters
    ----------
    start : int
        Starting position of the matrix slice to be tiled (inclusive, bins,
        0-based).
    stop : int
        End position of the matrix slice to be tiled (exclusive, bins, 0-based).
    edge : int
        Small edge around each tile to be included in the yielded coordinates.
    bandwidth : int
        Diagonal tiling is guaranteed to cover a diagonal are of width
        'bandwidth'.

    Yields
    ------
    Pairs of indices for every chunk use those indices [cstart:cstop) to fetch
    chunks from the cooler-object:
    >>> clr.matrix()[cstart:cstop, cstart:cstop]

    Notes
    -----
    Specify a [start,stop) region of a matrix you want to tile diagonally.
    Each slice is characterized by the coordinate of the top-left corner and
    size.

    * * * * * * * * * * *  0-th slice
    *       *           *
    *       *           *
    *   * * * * *       *  i-th slice
    *   *   *   *       *
    * * * * *   *       *
    *   *       *       *
    *   * * * * *       *
    *                   *
    *              ...  *  ...
    *                   *
    *                   *
    * * * * * * * * * * *

    These slices are supposed to cover up a diagonal band of size 'bandwidth'.

    """
    size = stop - start
    tiles = size // bandwidth + bool(size % bandwidth)

    # matrix parameters before chunking:
    if verbose:
        print("matrix of size {}X{} to be split so that\n".format(size,size)+
         "  diagonal region of size {} would be completely\n".format(bandwidth)+
         "  covered by the tiling, additionally keeping\n"+
         "  a small 'edge' of size w={}, to allow for\n".format(edge)+
         "  meaningfull convolution around boundaries.\n"+
         "  Resulting number of tiles is {}".format(tiles-1)+
         "  Non-edge case size of each tile is {}X{}".format(2*(bandwidth+edge),
                                                             2*(bandwidth+edge)))

    # actual number of tiles is tiles-1
    # by doing range(1, tiles) we are making sure we are processing the
    # upper-left chunk only once:
    for t in range(1, tiles):
        # l = max(0,M*t-M)
        # r = min(L,M*t+M)
        lw = max(0    , bandwidth*(t-1) - edge)
        rw = min(size , bandwidth*(t+1) + edge)
        # don't forget about the 'start' origin:
        yield lw+start, rw+start


def square_matrix_tiling(start, stop, step, edge, square=False, verbose=False):
    """
    Generate a stream of tiling coordinates that guarantee to cover an entire
    matrix. cis-signal only!

    Parameters
    ----------
    start : int
        Starting position of the matrix slice to be tiled (inclusive, bins,
        0-based).
    stop : int
        End position of the matrix slice to be tiled (exclusive, bins, 0-based).
    step : int
        Requested size of the tiles. Boundary tiles may or may not be of
        'step', see 'square'.
    edge : int
        Small edge around each tile to be included in the yielded coordinates.
    square : bool, optional

    Yields
    ------
    Pairs of indices for every chunk use those indices [cstart:cstop) to fetch
    chunks from the cooler-object:
    >>> clr.matrix()[cstart:cstop, cstart:cstop]

    Notes
    -----
    Each slice is characterized by the coordinate
    of the top-left corner and size.

    * * * * * * * * * * * * *
    *       *       *       *
    *       *       *  ...  *
    *       *       *       *
    * * * * * * * * *       *
    *       *               *
    *       *    ...        *
    *       *               *
    * * * * *               *
    *                       *
    *                       *
    *                       *
    * * * * * * * * * * * * *

    Square parameter determines behavior
    of the tiling function, when the
    size of the matrix is not an exact
    multiple of the 'step':

    square = False
    * * * * * * * * * * *
    *       *       *   *
    *       *       *   *
    *       *       *   *
    * * * * * * * * * * *
    *       *       *   *
    *       *       *   *
    *       *       *   *
    * * * * * * * * * * *
    *       *       *   *
    *       *       *   *
    * * * * * * * * * * *
    WARNING: be carefull with extracting
    expected in this case, as it is
    not trivial at all !!!

    square = True
    * * * * * * * * * * *
    *       *   *   *   *
    *       *   *   *   *
    *       *   *   *   *
    * * * * * * * * * * *
    *       *   *   *   *
    *       *   *   *   *
    * * * * * * * * * * *
    * * * * * * * * * * *
    *       *   *   *   *
    *       *   *   *   *
    * * * * * * * * * * *

    """
    size = stop - start
    tiles = size // step + bool(size % step)

    if verbose:
        print("matrix of size {}X{} to be splitted\n".format(size,size)+
         "  into square tiles of size {}.\n".format(step)+
         "  A small 'edge' of size w={} is added, to allow for\n".format(edge)+
         "  meaningfull convolution around boundaries.\n"+
         "  Resulting number of tiles is {}".format(tiles*tiles))

    for tx in range(tiles):
        for ty in range(tiles):

            lwx = max(0,    step*tx - edge)
            rwx = min(size, step*(tx+1) + edge)
            if square and (rwx >= size):
                lwx = size - step - edge

            lwy = max(0,    step*ty - edge)
            rwy = min(size, step*(ty+1) + edge)
            if square and (rwy >= size):
                lwy = size - step - edge

            yield (lwx+start,rwx+start), (lwy+start,rwy+start)


def _convolve_and_count_nans(O_bal,E_bal,E_raw,N_bal,kernel):
    """
    Dense versions of a bunch of matrices needed for convolution and
    calculation of number of NaNs in a vicinity of each pixel. And a kernel to
    be provided of course.

    """
    # a matrix filled with the kernel-weighted sums
    # based on a balanced observed matrix:
    KO = convolve(O_bal,
                  kernel,
                  mode='constant',
                  cval=0.0,
                  origin=0)
    # a matrix filled with the kernel-weighted sums
    # based on a balanced expected matrix:
    KE = convolve(E_bal,
                  kernel,
                  mode='constant',
                  cval=0.0,
                  origin=0)
    # get number of NaNs in a vicinity of every
    # pixel (kernel's nonzero footprint)
    # based on the NaN-matrix N_bal.
    # N_bal is shared NaNs between O_bal E_bal,
    # is it redundant ?
    NN = convolve(N_bal.astype(np.int),
                  # we have to use kernel's
                  # nonzero footprint:
                  (kernel != 0).astype(np.int),
                  mode='constant',
                  # there are only NaNs
                  # beyond the boundary:
                  cval=1,
                  origin=0)
    ######################################
    # using cval=0 for actual data and
    # cval=1 for NaNs matrix reduces
    # "boundary issue" to the "number of
    # NaNs"-issue
    # ####################################

    # now finally, E_raw*(KO/KE), as the
    # locally-adjusted expected with raw counts as values:
    Ek_raw = np.multiply(E_raw, np.divide(KO, KE))
    # return locally adjusted expected and number of NaNs
    # in the form of dense matrices:
    return Ek_raw, NN


########################################################################
# this should be a MAIN function to get locally adjusted expected
# Die Hauptfunktion
########################################################################
def get_adjusted_expected_tile_some_nans(origin,
                                         observed,
                                         expected,
                                         bal_weights,
                                         kernels,
                                         balance_factor=None,
                                         verbose=False):
    """
    'get_adjusted_expected_tile_some_nans', get locally adjusted
    expected for a collection of local-filters (kernels).

    Such locally adjusted expected, 'Ek' for a given kernel,
    can serve as a baseline for deciding whether a given
    pixel is enriched enough to call it a feature (dot-loop,
    flare, etc.) in a downstream analysis.

    For every pixel of interest [i,j], locally adjusted
    expected is a product of a global expected in that
    pixel E_bal[i,j] and an enrichment of local environ-
    ment of the pixel, described with a given kernel:
                              KERNEL[i,j](O_bal)
    Ek_bal[i,j] = E_bal[i,j]* ------------------
                              KERNEL[i,j](E_bal)
    where KERNEL[i,j](X) is a result of convolution
    between the kernel and a slice of matrix X centered
    around (i,j). See link below for details:
    https://en.wikipedia.org/wiki/Kernel_(image_processing)

    Returned values for observed and all expecteds
    are rescaled back to raw-counts, for the sake of
    downstream statistical analysis, which is using
    Poisson test to decide is a given pixel is enriched.
    (comparison between balanced values using Poisson-
    test is intractable):
                              KERNEL[i,j](O_bal)
    Ek_raw[i,j] = E_raw[i,j]* ------------------ ,
                              KERNEL[i,j](E_bal)
    where E_raw[i,j] is:
          1               1
    -------------- * -------------- * E_bal[i,j]
    bal_weights[i]   bal_weights[j]


    Parameters
    ----------
    origin : (int,int) tuple
        tuple of interegers that specify the
        location of an observed matrix slice.
        Measured in bins, not in nucleotides.
    observed : numpy.ndarray
        square symmetrical dense-matrix
        that contains balanced observed O_bal
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        should we switch to RAW here ?
        it would be easy for the output ....
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    expected : numpy.ndarray
        square symmetrical dense-matrix
        that contains expected, calculated
        based on balanced observed: E_bal.
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        should we switch to its 1D representation here ?
        good for now, but expected might change later ...
        Tanay's expected, expected with modeled TAD's etc ...
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    bal_weights : numpy.ndarray or (numpy.ndarray, numpy.ndarray)
        1D vector used to turn raw observed
        into balanced observed for a slice of
        a matrix with the origin on the diagonal;
        and a tuple/list of a couple of 1D arrays
        in case it is a slice with an arbitrary
        origin.
    kernels : dict of (str, numpy.ndarray)
        dictionary of kernels/masks to perform
        convolution of the heatmap. Kernels
        describe the local environment, and
        used to estimate baseline for finding
        enriched/prominent peaks.
        Peak must be enriched with respect to
        all local environments (all kernels),
        to be considered significant.
        Dictionay keys must contain names for
        each kernel.
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        Beware!: kernels are flipped and
        only then multiplied to matrix by
        scipy.ndimage.convolve
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    balance_factor: float
        Multiplicative Balancing factor:
        balanced matrix multiplied by this factor
        sums up to the total number of reads (taking
        symmetry into account) instead of number of
        bins in matrix. defaults to None and skips
        a lowleft KernelObserved factor calculation.
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        We need this to test how dynamic donut size
        is affecting peak calling results.
        !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    verbose: bool
        Set to True to print some progress
        messages to stdout.

    Returns
    -------
    peaks_df : pandas.DataFrame
        sparsified DataFrame that stores results of
        locally adjusted calculations for every kernel
        for a given slice of input matrix. Multiple
        instences of such 'peaks_df' can be concatena-
        ted and deduplicated for the downstream analysis.
        Reported columns:
        bin1_id - bin1_id index (row), adjusted to origin
        bin2_id - bin bin2_id index, adjusted to origin
        la_exp - locally adjusted expected (for each kernel)
        la_nan - number of NaNs around (each kernel's footprint)
        exp.raw - global expected, rescaled to raw-counts
        obs.raw - observed values in raw-counts.

    """
    # extract origin coordinate of this tile:
    io, jo = origin
    # let's extract full matrices and ice_vector:
    O_raw = observed # raw observed, no need to copy, no modifications.
    E_bal = np.copy(expected)
    # 'bal_weights': ndarray or a couple of those ...
    if isinstance(bal_weights, np.ndarray):
        v_bal_i = bal_weights
        v_bal_j = bal_weights
    elif isinstance(bal_weights, (tuple,list)):
        v_bal_i,v_bal_j = bal_weights
    else:
        raise ValueError("'bal_weights' must be an numpy.ndarray"
                    "for slices of a matrix with diagonal-origin or"
                    "a tuple/list of a couple of numpy.ndarray-s"
                    "for a slice of matrix with an arbitrary origin.")
    # kernels must be a dict with kernel-names as keys
    # and kernel ndarrays as values.
    if not isinstance(kernels, dict):
        raise ValueError("'kernels' must be a dictionary"
                    "with name-keys and ndarrays-values.")

    # balanced observed, from raw-observed
    # by element-wise multiply:
    O_bal = np.multiply(O_raw, np.outer(v_bal_i,v_bal_j))
    # O_bal is separate from O_raw memory-wise.

    # fill lower triangle of O_bal and E_bal with NaNs
    # in order to prevent peak calling from the lower triangle
    # and also to provide fair locally adjusted expected
    # estimation for pixels very close to diagonal, whose
    # "donuts"(kernels) would be crossing the main diagonal.
    # The trickiest thing here would be dealing with the origin: io,jo.
    O_bal[np.tril_indices_from(O_bal,k=(io-jo)-1)] = np.nan
    E_bal[np.tril_indices_from(E_bal,k=(io-jo)-1)] = np.nan

    # raw E_bal: element-wise division of E_bal[i,j] and
    # v_bal[i]*v_bal[j]:
    E_raw = np.divide(E_bal, np.outer(v_bal_i,v_bal_j))

    # let's calculate a matrix of common NaNs
    # shared between observed and expected:
    # check if it's redundant ? (is NaNs from O_bal sufficient? )
    N_bal = np.logical_or(np.isnan(O_bal),
                          np.isnan(E_bal))
    # fill in common nan-s with zeroes, preventing
    # NaNs during convolution part of '_convolve_and_count_nans':
    O_bal[N_bal] = 0.0
    E_bal[N_bal] = 0.0
    # think about usinf copyto and where functions later:
    # https://stackoverflow.com/questions/6431973/how-to-copy-data-from-a-numpy-array-to-another
    #
    #
    # we are going to accumulate all the results
    # into a DataFrame, keeping NaNs, and other
    # unfiltered results (even the lower triangle for now):
    i,j = np.indices(O_raw.shape)
    # pack it into DataFrame to accumulate results:
    peaks_df = pd.DataFrame({"bin1_id": i.flatten()+io,
                             "bin2_id": j.flatten()+jo})

    with np.errstate(divide='ignore', invalid='ignore'):
        for kernel_name, kernel in kernels.items():
            ###############################
            # kernel-specific calculations:
            ###############################
            # kernel paramters such as width etc
            # are taken into account implicitly ...
            ########################################
            #####################
            # unroll _convolve_and_count_nans function back
            # for us to test the dynamic donut criteria ...
            #####################
            # Ek_raw, NN = _convolve_and_count_nans(O_bal,
            #                                     E_bal,
            #                                     E_raw,
            #                                     N_bal,
            #                                     kernel)
            # Dense versions of a bunch of matrices needed for convolution and
            # calculation of number of NaNs in a vicinity of each pixel. And a kernel to
            # be provided of course.
            # a matrix filled with the kernel-weighted sums
            # based on a balanced observed matrix:
            KO = convolve(O_bal,
                          kernel,
                          mode='constant',
                          cval=0.0,
                          origin=0)
            # a matrix filled with the kernel-weighted sums
            # based on a balanced expected matrix:
            KE = convolve(E_bal,
                          kernel,
                          mode='constant',
                          cval=0.0,
                          origin=0)
            # get number of NaNs in a vicinity of every
            # pixel (kernel's nonzero footprint)
            # based on the NaN-matrix N_bal.
            # N_bal is shared NaNs between O_bal E_bal,
            NN = convolve(N_bal.astype(np.int),
                          # we have to use kernel's
                          # nonzero footprint:
                          (kernel != 0).astype(np.int),
                          mode='constant',
                          # there are only NaNs
                          # beyond the boundary:
                          cval=1,
                          origin=0)
            ######################################
            # using cval=0 for actual data and
            # cval=1 for NaNs matrix reduces
            # "boundary issue" to the "number of
            # NaNs"-issue
            # ####################################
            # now finally, E_raw*(KO/KE), as the
            # locally-adjusted expected with raw counts as values:
            Ek_raw = np.multiply(E_raw, np.divide(KO, KE))

            # this is the place where we would need to extract
            # some results of convolution and multuplt it by the
            # appropriate factor "cooler._load_attrs(‘bins/weight’)[‘scale’]" ...
            if balance_factor and (kernel_name == "lowleft"):
                peaks_df["factor_balance."+kernel_name+".KerObs"] = balance_factor * KO.flatten()
                # KO*balance_factor: to be compared with 16 ...
            if verbose:
                print("Convolution with kernel {} is complete.".format(kernel_name))
            #
            # accumulation into single DataFrame:
            # store locally adjusted expected for each kernel
            # and number of NaNs in the footprint of each kernel
            peaks_df["la_exp."+kernel_name+".value"] = Ek_raw.flatten()
            peaks_df["la_exp."+kernel_name+".nnans"] = NN.flatten()
            # do all the filter/logic/masking etc on the complete DataFrame ...
    #####################################
    # downstream stuff is supposed to be
    # aggregated over all kernels ...
    #####################################
    peaks_df["exp.raw"] = E_raw.flatten()
    peaks_df["obs.raw"] = O_raw.flatten()

    # TO BE REFACTORED/deprecated ...
    # compatibility with legacy API is completely BROKEN
    # post-processing allows us to restore it, see tests,
    # but we pay with the processing speed for it.
    mask_ndx = pd.Series(0, index=peaks_df.index, dtype=np.bool)
    for kernel_name, kernel in kernels.items():
        # accummulating with a vector full of 'False':
        mask_ndx_kernel = ~np.isfinite(peaks_df["la_exp."+kernel_name+".value"])
        mask_ndx = np.logical_or(mask_ndx_kernel,mask_ndx)

    # returning only pixels from upper triangle of a matrix
    # is likely here to stay:
    upper_band = (peaks_df["bin1_id"] < peaks_df["bin2_id"])
    # Consider filling lower triangle of the OBSERVED matrix tile
    # with NaNs, instead of this - we'd need this for a fair
    # consideration of the pixels that are super close to the
    # diagonal and in a case, when the corresponding donut would
    # cross a diagonal line.
    # selecting pixels in relation to diagonal - too far, too
    # close etc, is now shifted to the outside of this function
    # a way to simplify code.


    # return good semi-sparsified DF:
    return peaks_df[~mask_ndx & upper_band].reset_index(drop=True)


def heatmap_tiles_generator_diag(clr, chroms, pad_size, tile_size, band_to_cover):
    """
    A generator yielding heatmap tiles that are needed to cover the requested
    band_to_cover around diagonal. Each tile is "padded" with pad_size edge to
    allow proper kernel-convolution of pixels close to boundary.

    Parameters
    ----------
    clr : cooler
        Cooler object to use to extract chromosome extents.
    chroms : iterable
        Iterable of chromosomes to process
    pad_size : int
        Size of padding around each tile. Typically the outer size of the
        kernel.
    tile_size : int
        Size of the heatmap tile.
    band_to_cover : int
        Size of the diagonal band to be covered by the generated tiles.
        Typically correspond to the max_loci_separation for called dots.

    Returns
    -------
    tile : tuple
        Generator of tuples of three, which contain
        chromosome name, row index of the tile,
        column index of the tile (chrom, tilei, tilej).

    """

    for chrom in chroms:
        chr_start, chr_stop = clr.extent(chrom)
        for tilei, tilej in square_matrix_tiling(chr_start,
                                                 chr_stop,
                                                 tile_size,
                                                 pad_size):
            # check if a given tile intersects with
            # with the diagonal band of interest ...
            diag_from = tilej[0] - tilei[1]
            diag_to   = tilej[1] - tilei[0]
            #
            band_from = 0
            band_to   = band_to_cover
            # we are using this >2*padding trick to exclude
            # tiles from the lower triangle from calculations ...
            if (min(band_to,diag_to) - max(band_from,diag_from)) > 2*pad_size:
                yield chrom, tilei, tilej


def score_tile(tile_cij, clr, cis_exp, exp_v_name, bal_v_name, kernels,
               nans_tolerated, band_to_cover, balance_factor, verbose):
    """
    The main working function that given a tile of a heatmap, applies kernels to
    perform convolution to calculate locally-adjusted expected and then
    calculates a p-value for every meaningfull pixel against these l.a. expected
    values.

    Parameters
    ----------
    tile_cij : tuple
        Tuple of 3: chromosome name, tile span row-wise, tile span column-wise:
        (chrom, tile_i, tile_j), where tile_i = (start_i, end_i), and
        tile_j = (start_j, end_j).
    clr : cooler
        Cooler object to use to extract Hi-C heatmap data.
    cis_exp : pandas.DataFrame
        DataFrame with 1 dimensional expected, indexed with 'chrom' and 'diag'.
    exp_v_name : str
        Name of a value column in expected DataFrame
    bal_v_name : str
        Name of a value column with balancing weights in a cooler.bins()
        DataFrame. Typically 'weight'.
    kernels : dict
        A dictionary with keys being kernels names and values being ndarrays
        representing those kernels.
    nans_tolerated : int
        Number of NaNs tolerated in a footprint of every kernel.
    band_to_cover : int
        Results would be stored only for pixels connecting loci closer than
        'band_to_cover'.
    balance_factor : float
        Balancing factor to turn sum of balanced matrix back approximately
        to the number of pairs (used for dynamic-donut criteria mostly).
        use None value to disable dynamic-donut criteria calculation.
    verbose : bool
        Enable verbose output.

    Returns
    -------
    res_df : pandas.DataFrame
        results: annotated pixels with calculated locally adjusted expected
        for every kernels, observed, precalculated pvalues, number of NaNs in
        footprint of every kernels, all of that in a form of an annotated
        pixels DataFrame for eligible pixels of a given tile.

    """
    # unpack tile's coordinates
    chrom, tilei, tilej = tile_cij
    origin = (tilei[0], tilej[0])

    # we have to do it for every tile, because
    # chrom is not known apriori (maybe move outside):
    lazy_exp = LazyToeplitz(cis_exp.loc[chrom][exp_v_name].values)

    # RAW observed matrix slice:
    observed = clr.matrix(balance=False)[slice(*tilei), slice(*tilej)]
    # expected as a rectangular tile :
    expected = lazy_exp[slice(*tilei), slice(*tilej)]
    # slice of balance_weight for row-span and column-span :
    bal_weight_i = clr.bins()[slice(*tilei)][bal_v_name].values
    bal_weight_j = clr.bins()[slice(*tilej)][bal_v_name].values

    # do the convolutions
    result = get_adjusted_expected_tile_some_nans(
        origin=origin,
        observed=observed,
        expected=expected,
        bal_weights=(bal_weight_i,bal_weight_j),
        kernels=kernels,
        balance_factor=balance_factor,
        verbose=verbose)

    # Post-processing filters
    # (1) exclude pixels that connect loci further than 'band_to_cover' apart:
    is_inside_band = (result["bin1_id"] > (result["bin2_id"]-band_to_cover))

    # (2) identify pixels that pass number of NaNs compliance test for ALL kernels:
    does_comply_nans = np.all(
        result[["la_exp."+k+".nnans" for k in kernels]] < nans_tolerated,
        axis=1)
    # so, selecting inside band and nNaNs compliant results:
    # ( drop dropping index maybe ??? ) ...
    res_df = result[is_inside_band & does_comply_nans].reset_index(drop=True)
    ########################################################################
    # consider retiring Poisson testing from here, in case we
    # stick with l-chunking or opposite - add histogramming business here(!)
    ########################################################################
    # do Poisson tests:
    get_pval = lambda la_exp : 1.0 - poisson.cdf(res_df["obs.raw"], la_exp)
    for k in kernels:
        res_df["la_exp."+k+".pval"] = get_pval( res_df["la_exp."+k+".value"] )

    # annotate and return
    return cooler.annotate(res_df.reset_index(drop=True), clr.bins()[:])


def histogram_scored_pixels(scored_df, kernels, ledges, verbose):
    """
    An attempt to implement HiCCUPS-like lambda-chunking
    statistical procedure.
    This function aims at building up a histogram of locally
    adjusted expected scores for groups of characterized
    pixels.
    Such histograms are later used to compute FDR thresholds
    for different "classes" of hypothesis (classified by their
    l.a. expected scores).

    Parameters
    ----------
    scored_df : pd.DataFrame
        A table with the scoring information for a group of pixels.
    kernels : dict
        A dictionary with keys being kernels names and values being ndarrays
        representing those kernels.
    ledges : ndarray
        An ndarray with bin lambda-edges for groupping loc. adj. expecteds,
        i.e., classifying statistical hypothesis into lambda-classes.
        Left-most bin (-inf, 1], and right-most one (value,+inf].
    verbose : bool
        Enable verbose output.

    Returns
    -------
    hists : dict of pandas.DataFrame
        A dictionary of pandas.DataFrame with lambda/observed histogram for
        every kernel-type.


    Notes
    -----
    This is just an attempt to implement HiCCUPS-like lambda-chunking.
    So we'll be returning histograms corresponding to the chunks of
    scored pixels.
    Consider modifying/accumulation a globally defined hists object,
    or probably making something like a Feature2D class later on
    where hists would be a class feature and histogramming_step would be
    a method.


    """

    # lambda-chunking implies different 'pval' calculation
    # procedure with a single Poisson expected for all the
    # hypothesis in a same "class", i.e. with the l.a. expecteds
    # from the same histogram bin.


    ########################
    # implementation ideas:
    ########################
    # same observations/hypothesis needs to be classified according
    # to different l.a. expecteds (i.e. for different kernel-types),
    # which can be done with a pandas groupby, something like that:
    # https://stackoverflow.com/questions/21441259
    #
    # after that we could iterate over groups and do np.bincout on
    # the "observed.raw" column (assuming it's of integer type) ...

    hists = {}
    for k in kernels:
        # verbose:
        if verbose:
            print("Building a histogram for kernel-type {}".format(k))
        #  we would need to generate a bunch of these histograms for all of the
        # kernel types:
        # needs to be lambda-binned             : scored_df["la_exp."+k+".value"]
        # needs to be histogrammed in every bin : scored_df["obs.raw"]
        #
        # lambda-bin index for kernel-type "k":
        lbins = pd.cut(scored_df["la_exp."+k+".value"],ledges)
        # now for each lambda-bin construct a histogramm of "obs.raw":
        obs_hist = {}
        for lbin, grp_df in scored_df.groupby(lbins):
            # check if obs.raw is integer of spome kind (temporary):
            assert np.issubdtype(grp_df["obs.raw"].dtype, np.integer)
            # perform bincounting ...
            obs_hist[lbin] = pd.Series(np.bincount(grp_df["obs.raw"]))
            # ACHTUNG! assigning directly to empty DF leads to data loss!
            # turn ndarray in Series for ease of handling, i.e.
            # assign a bunch of columns of different sizes to DataFrame.
            #
            # Consider updating global "hists" later on, or implementing a
            # special class for that. Mind that Python multiprocessing
            # "threads" are processes and thus cannot share/modify a shared
            # memory location - deal with it, maybe dask-something ?!
            #
            # turned out that storing W1xW2 for every "thread" requires a ton
            # of memory - that's why different sizes... @nvictus ?
        # store W1x(<=W2) hist for every kernel-type:
        hists[k] = pd.DataFrame(obs_hist).fillna(0).astype(np.integer)
    # return a dict of DataFrames with a bunch of histograms:
    return hists


def extract_scored_pixels(scored_df, kernels, thresholds, ledges, verbose):
    """
    An attempt to implement HiCCUPS-like lambda-chunking
    statistical procedure.
    Use FDR thresholds for different "classes" of hypothesis
    (classified by their l.a. expected scores), in order to
    extract pixels that stand out.

    Parameters
    ----------
    scored_df : pd.DataFrame
        A table with the scoring information for a group of pixels.
    kernels : dict
        A dictionary with keys being kernel names and values being ndarrays
        representing those kernels.
    thresholds : dict
        A dictionary with keys being kernel names and values pandas.Series
        indexed with Intervals defined by 'ledges' boundaries and storing FDR
        thresholds for observed values.
    ledges : ndarray
        An ndarray with bin lambda-edges for groupping loc. adj. expecteds,
        i.e., classifying statistical hypothesis into lambda-classes.
        Left-most bin (-inf, 1], and right-most one (value,+inf].
    verbose : bool
        Enable verbose output.

    Returns
    -------
    scored_df_slice : pandas.DataFrame
        Filtered DataFrame of pixels extracted applying FDR thresholds.

    Notes
    -----
    This is just an attempt to implement HiCCUPS-like lambda-chunking.

    """
    comply_fdr_list = np.ones(len(scored_df), dtype=np.bool)

    for k in kernels:
        # using special features of IntervalIndex:
        # http://pandas.pydata.org/pandas-docs/stable/advanced.html#intervalindex
        # i.e. IntervalIndex can be .loc-ed with values that would be
        # corresponded with their Intervals (!!!):
        comply_fdr_k = (scored_df["obs.raw"].values > \
                        thresholds[k].loc[scored_df["la_exp."+k+".value"]].values)
        ## attempting to extract q-values using l-chunks and IntervalIndex:
        ## we'll do it in an ugly but workign fashion, by simply
        ## iteration over pairs of obs, la_exp and extracting needed qvals
        ## one after another ...
        #scored_df["la_exp."+k+".qval"] = \
        #        [ qvalues[k].loc[o,e] for o,e \
        #            in scored_df[["obs.raw","la_exp."+k+".value"]].itertuples(index=False) ]
        ##
        ## accumulate comply_fdr_k into comply_fdr_list
        # using np.logical_and:
        comply_fdr_list = np.logical_and(comply_fdr_list, comply_fdr_k)
    # return a slice of 'scored_df' that complies FDR thresholds:
    return scored_df[comply_fdr_list]


def scoring_step(clr, expected, expected_name, tiles, kernels,
                 max_nans_tolerated, loci_separation_bins, output_path,
                 nproc, verbose):
    if verbose:
        print("Preparing to convolve {} tiles:".format(len(tiles)))

    # add very_verbose to supress output from convolution of every tile
    very_verbose = False
    job = partial(
        score_tile,
        clr=clr,
        cis_exp=expected,
        exp_v_name=expected_name,
        bal_v_name='weight',
        kernels=kernels,
        nans_tolerated=max_nans_tolerated,
        band_to_cover=loci_separation_bins,
        verbose=very_verbose)

    if nproc > 1:
        pool = mp.Pool(nproc)
        map_ = pool.imap
        map_kwargs = dict(chunksize=int(np.ceil(len(tiles)/nproc)))
        if verbose:
            print("creating a Pool of {} workers to tackle {} tiles".format(
                    nproc, len(tiles)))
    else:
        map_ = map
        if verbose:
            print("fallback to serial implementation.")
        map_kwargs = {}
    try:
        # consider using
        # https://github.com/mirnylab/cooler/blob/9e72ee202b0ac6f9d93fd2444d6f94c524962769/cooler/tools.py#L59
        # here:
        chunks = map_(job, tiles, **map_kwargs)
        append = False
        for chunk in chunks:
            chunk.to_hdf(output_path,
                         key='results',
                         format='table',
                         append=append)
            append = True
    finally:
        if nproc > 1:
            pool.close()


def scoring_and_histogramming_step(clr, expected, expected_name, tiles, kernels,
                                   ledges, max_nans_tolerated, loci_separation_bins,
                                   output_path, nproc, verbose):
    """
    This is a derivative of the 'scoring_step'
    which is supposed to implement the 1st of the
    lambda-chunking procedure - histogramming.

    Basically we are piping scoring operation
    together with histogramming into a single
    pipeline of per-chunk operations/transforms.
    """
    if verbose:
        print("Preparing to convolve {} tiles:".format(len(tiles)))

    # add very_verbose to supress output from convolution of every tile
    very_verbose = False

    # to score per tile:
    to_score = partial(
        score_tile,
        clr=clr,
        cis_exp=expected,
        exp_v_name=expected_name,
        bal_v_name='weight',
        kernels=kernels,
        nans_tolerated=max_nans_tolerated,
        band_to_cover=loci_separation_bins,
        # do not calculate dynamic-donut criteria
        # for now.
        balance_factor=None,
        verbose=very_verbose)

    # to hist per scored chunk:
    to_hist = partial(
        histogram_scored_pixels,
        kernels=kernels,
        ledges=ledges,
        verbose=very_verbose)

    # composing/piping scoring and histogramming
    # together :
    job = lambda tile : to_hist(to_score(tile))

    # copy paste from @nvictus modified 'scoring_step':
    if nproc > 1:
        pool = mp.Pool(nproc)
        map_ = pool.imap
        map_kwargs = dict(chunksize=int(np.ceil(len(tiles)/nproc)))
        if verbose:
            print("creating a Pool of {} workers to tackle {} tiles".format(
                    nproc, len(tiles)))
    else:
        map_ = map
        if verbose:
            print("fallback to serial implementation.")
        map_kwargs = {}
    try:
        # consider using
        # https://github.com/mirnylab/cooler/blob/9e72ee202b0ac6f9d93fd2444d6f94c524962769/cooler/tools.py#L59
        # here:
        hchunks = map_(job, tiles, **map_kwargs)
        # hchunks TO BE ACCUMULATED
        # hopefully 'hchunks' would stay in memory
        # until we would get a chance to accumulate them:
    finally:
        if nproc > 1:
            pool.close()
    #
    # now we need to combine/sum all of the histograms
    # for different kernels:
    #
    # assuming we know "kernels"
    # this is very ugly, but ok
    # for the draft lambda-chunking
    # lambda version of lambda-chunking:
    def _sum_hists(hx,hy):
        # perform a DataFrame summation
        # for every value of the dictionary:
        hxy = {}
        for k in kernels:
            hxy[k] = hx[k].add(hy[k],fill_value=0).astype(np.integer)
        # returning the sum:
        return hxy

    # ######################################################
    # this approach is tested and at the very least
    # number of pixels in a dump list matches
    # with the .sum().sum() of the histogram
    # both for 10kb and 5kb,
    # thus we should consider this as a reference
    # implementation, albeit not a very efficient one ...
    # ######################################################
    final_hist = reduce(_sum_hists, hchunks)
    # we have to make sure there is nothing in the
    # top bin, i.e., there are no l.a. expecteds > base^(len(ledges)-1)
    for k in kernels:
        last_la_exp_bin = final_hist[k].columns[-1]
        last_la_exp_vals = final_hist[k].iloc[:,-1]
        # checking the top bin:
        assert (last_la_exp_vals.sum()==0), \
                "There are la_exp.{}.value in {}, please check the histogram" \
                                                    .format(k,last_la_exp_bin)
        # drop that last column/bin (last_edge, +inf]:
        final_hist[k] = final_hist[k].drop(columns=last_la_exp_bin)
        # consider dropping all of the columns that have zero .sum()
    # returning filtered histogram
    return final_hist


def scoring_and_extraction_step(clr, expected, expected_name, tiles, kernels,
                               ledges, thresholds, max_nans_tolerated,
                               balance_factor, loci_separation_bins, output_path,
                               nproc, verbose):
    """
    This is a derivative of the 'scoring_step'
    which is supposed to implement the 2nd of the
    lambda-chunking procedure - extracting pixels
    that are FDR compliant.

    Basically we are piping scoring operation
    together with extraction into a single
    pipeline of per-chunk operations/transforms.
    """
    if verbose:
        print("Preparing to convolve {} tiles:".format(len(tiles)))

    # add very_verbose to supress output from convolution of every tile
    very_verbose = False

    # to score per tile:
    to_score = partial(
        score_tile,
        clr=clr,
        cis_exp=expected,
        exp_v_name=expected_name,
        bal_v_name='weight',
        kernels=kernels,
        nans_tolerated=max_nans_tolerated,
        band_to_cover=loci_separation_bins,
        balance_factor=balance_factor,
        verbose=very_verbose)

    # to hist per scored chunk:
    to_extract = partial(
        extract_scored_pixels,
        kernels=kernels,
        thresholds=thresholds,
        ledges=ledges,
        verbose=very_verbose)

    # composing/piping scoring and histogramming
    # together :
    job = lambda tile : to_extract(to_score(tile))

    # copy paste from @nvictus modified 'scoring_step':
    if nproc > 1:
        pool = mp.Pool(nproc)
        map_ = pool.imap
        map_kwargs = dict(chunksize=int(np.ceil(len(tiles)/nproc)))
        if verbose:
            print("creating a Pool of {} workers to tackle {} tiles".format(
                    nproc, len(tiles)))
    else:
        map_ = map
        if verbose:
            print("fallback to serial implementation.")
        map_kwargs = {}
    try:
        # consider using
        # https://github.com/mirnylab/cooler/blob/9e72ee202b0ac6f9d93fd2444d6f94c524962769/cooler/tools.py#L59
        # here:
        filtered_pix_chunks = map_(job, tiles, **map_kwargs)
        significant_pixels = pd.concat(filtered_pix_chunks,ignore_index=True)
        if output_path is not None:
            significant_pixels.to_csv(output_path,
                                      sep='\t',
                                      header=True,
                                      index=False,
                                      compression=None)
    finally:
        if nproc > 1:
            pool.close()
    # # concat and store the results if needed:
    # significant_pixels = pd.concat(filtered_pix_chunks)
    return significant_pixels \
                .sort_values(by=["chrom1","chrom2","start1","start2"]) \
                .reset_index(drop=True)



def clustering_step_local(scores_df, expected_chroms,
                          dots_clustering_radius, verbose):
    """
    This is a new "clustering" step updated for the pixels
    processed by lambda-chunking multiple hypothesis testing.

    This method assumes that 'scores_df' is a DataFrame with
    all of the pixels that needs to be clustered, thus there is
    no additional 'comply_fdr' column and selection of compliant
    pixels.

    This step is a clustering-only (using Birch from scikit).

    Parameters
    ----------
    scores_df : pandas.DataFrame
        DataFrame that stores filtered pixels that are ready to be
        clustered, no more 'comply_fdr' column dependency.
    expected_chroms : iterable
        An iterable of chromosomes to be clustered.
    dots_clustering_radius : int
        Birch-clustering threshold.
    verbose : bool
        Enable verbose output.

    Returns
    -------
    centroids : pandas.DataFrame
        Pixels from 'scores_df' annotated with clustering
        information.

    Notes
    -----
    'dots_clustering_radius' in Birch clustering algorithm
    corresponds to a double the clustering radius in the
    "greedy"-clustering used in HiCCUPS (to be tested).

    """

    # using different bin12_id_names since all
    # pixels are annotated at this point.
    pixel_clust_list = []
    for chrom  in expected_chroms:
        # probably generate one big DataFrame with clustering
        # information only and then just merge it with the
        # existing 'scores_df'-DataFrame.
        # should we use groupby instead of 'scores_df['chrom12']==chrom' ?!
        # to be tested ...
        df = scores_df[((scores_df['chrom1'].astype(str)==str(chrom)) &
                        (scores_df['chrom2'].astype(str)==str(chrom)))]
        if not len(df):
            continue

        pixel_clust = clust_2D_pixels(
            df,
            threshold_cluster=dots_clustering_radius,
            bin1_id_name='start1',
            bin2_id_name='start2',
            verbose=verbose)
        pixel_clust_list.append(pixel_clust)
    if verbose:
        print("Clustering is over!")
    # concatenate clustering results ...
    # indexing information persists here ...
    pixel_clust_df = pd.concat(pixel_clust_list, ignore_index=False)

    # now merge pixel_clust_df and scores_df DataFrame ...
    # # and merge (index-wise) with the main DataFrame:
    df = pd.merge(
        scores_df,
        pixel_clust_df,
        how='left',
        left_index=True,
        right_index=True)

    # report only centroids with highest Observed:
    chrom_clust_group = df.groupby(["chrom1", "chrom2", "c_label"])
    centroids = df.loc[chrom_clust_group["obs.raw"].idxmax()]
    return centroids


def clustering_step(scores_file, expected_chroms, ktypes, fdr,
                    dots_clustering_radius, verbose):
    """
    This is an old "clustering" step, before lambda-chunking
    was implemented.
    This step actually includes both multiple hypothesis
    testing (its simple genome-wide version of BH-FDR) and
    a clustering step itself (using Birch from scikit).
    This method also assumes 'scores_file' to be an external
    hdf file, and it would try to read the entire file in
    memory.
    """
    res_df = pd.read_hdf(scores_file, 'results')

    # do Benjamin-Hochberg FDR multiple hypothesis tests
    # genome-wide:
    for k in ktypes:
        res_df["la_exp."+k+".qval"] = get_qvals( res_df["la_exp."+k+".pval"] )

    # combine results of all tests:
    res_df['comply_fdr'] = np.all(
        res_df[["la_exp."+k+".qval" for k in ktypes]] <= fdr,
        axis=1)

    # print a message for timing:
    if verbose:
        print("Genome-wide multiple hypothesis testing is done.")

    # using different bin12_id_names since all
    # pixels are annotated at this point.
    pixel_clust_list = []
    for chrom  in expected_chroms:
        # probably generate one big DataFrame with clustering
        # information only and then just merge it with the
        # existing 'res_df'-DataFrame.
        # should we use groupby instead of 'res_df['chrom12']==chrom' ?!
        # to be tested ...
        df = res_df[(res_df['comply_fdr'] &
                    (res_df['chrom1']==chrom) &
                    (res_df['chrom2']==chrom))]

        pixel_clust = clust_2D_pixels(
            df,
            threshold_cluster=dots_clustering_radius,
            bin1_id_name='start1',
            bin2_id_name='start2',
            verbose=verbose)
        pixel_clust_list.append(pixel_clust)
    if verbose:
        print("Clustering is over!")
    # concatenate clustering results ...
    # indexing information persists here ...
    pixel_clust_df = pd.concat(pixel_clust_list, ignore_index=False)

    # now merge pixel_clust_df and res_df DataFrame ...
    # # and merge (index-wise) with the main DataFrame:
    df = pd.merge(
        res_df[res_df['comply_fdr']],
        pixel_clust_df,
        how='left',
        left_index=True,
        right_index=True)

    # report only centroids with highest Observed:
    chrom_clust_group = df.groupby(["chrom1", "chrom2", "c_label"])
    centroids = df.loc[chrom_clust_group["obs.raw"].idxmax()]
    return centroids


def thresholding_step(centroids):
    # (2)
    # filter by FDR, enrichment etc:
    enrichment_factor_1 = 1.5
    enrichment_factor_2 = 1.75
    enrichment_factor_3 = 2.0
    FDR_orphan_threshold = 0.02
    ######################################################################
    # # Temporarily remove orphans filtering, until q-vals are calculated:
    ######################################################################
    enrichment_fdr_comply = (
        (centroids["obs.raw"] > enrichment_factor_2 * centroids["la_exp.lowleft.value"]) &
        (centroids["obs.raw"] > enrichment_factor_2 * centroids["la_exp.donut.value"]) &
        (centroids["obs.raw"] > enrichment_factor_1 * centroids["la_exp.vertical.value"]) &
        (centroids["obs.raw"] > enrichment_factor_1 * centroids["la_exp.horizontal.value"]) &
        ( (centroids["obs.raw"] > enrichment_factor_3 * centroids["la_exp.lowleft.value"])
            | (centroids["obs.raw"] > enrichment_factor_3 * centroids["la_exp.donut.value"]) ) &
        ( (centroids["c_size"] > 1)
           | ((centroids["la_exp.lowleft.qval"]
               + centroids["la_exp.donut.qval"]
               + centroids["la_exp.vertical.qval"]
               + centroids["la_exp.horizontal.qval"]) <= FDR_orphan_threshold)
        )
    )
    # #
    # enrichment_fdr_comply = (
    #     (centroids["obs.raw"] > enrichment_factor_2 * centroids["la_exp.lowleft.value"]) &
    #     (centroids["obs.raw"] > enrichment_factor_2 * centroids["la_exp.donut.value"]) &
    #     (centroids["obs.raw"] > enrichment_factor_1 * centroids["la_exp.vertical.value"]) &
    #     (centroids["obs.raw"] > enrichment_factor_1 * centroids["la_exp.horizontal.value"]) &
    #     ( (centroids["obs.raw"] > enrichment_factor_3 * centroids["la_exp.lowleft.value"])
    #         | (centroids["obs.raw"] > enrichment_factor_3 * centroids["la_exp.donut.value"]) )
    # )
    # use "enrichment_fdr_comply" to filter out
    # non-satisfying pixels:
    out = centroids[enrichment_fdr_comply]

    # # # columns up for grabs, take whatever you need:
    # chrom1
    # start1
    # end1
    # weight1
    # chrom2
    # start2
    # end2
    # weight2
    # la_exp.donut.value
    # la_exp.donut.nnans
    # la_exp.vertical.value
    # la_exp.vertical.nnans
    # la_exp.horizontal.value
    # la_exp.horizontal.nnans
    # la_exp.lowleft.value
    # la_exp.lowleft.nnans
    # la_exp.upright.value
    # la_exp.upright.nnans
    # exp.raw
    # obs.raw
    # la_exp.donut.pval
    # la_exp.vertical.pval
    # la_exp.horizontal.pval
    # la_exp.lowleft.pval
    # la_exp.upright.pval
    # la_exp.donut.qval
    # la_exp.vertical.qval
    # la_exp.horizontal.qval
    # la_exp.lowleft.qval
    # la_exp.upright.qval
    # comply_fdr
    # cstart1
    # cstart2
    # c_label
    # c_size

    # ...
    # to be added to the list of output columns:
    # "factor_balance."+"lowleft"+".KerObs"
    # ...

    # tentaive output columns list:
    columns_for_output = [
        'chrom1',
        'start1',
        'end1',
        'chrom2',
        'start2',
        'end2',
        'cstart1',
        'cstart2',
        'c_label',
        'c_size',
        'obs.raw',
        'exp.raw',
        'la_exp.donut.value',
        'la_exp.vertical.value',
        'la_exp.horizontal.value',
        'la_exp.lowleft.value',
        "factor_balance.lowleft.KerObs",
        # 'la_exp.upright.value',
        # 'la_exp.upright.qval',
        'la_exp.donut.qval',
        'la_exp.vertical.qval',
        'la_exp.horizontal.qval',
        'la_exp.lowleft.qval'
    ]
    return out[columns_for_output]
