import itertools

import numpy as np
from pycompss.api.api import compss_delete_object, compss_wait_on
from pycompss.api.parameter import COLLECTION_OUT, Type, Depth, \
    COLLECTION_INOUT, COLLECTION_IN
from pycompss.api.task import task

from dislib.data.array import Array, identity


def kron(a, b, block_size=None):
    """ Kronecker product of two ds-arrays.

    Parameters
    ----------
    a, b : ds-arrays
        Input ds-arrays.
    block_size : tuple of two ints, optional
        Block size of the resulting array. Defaults to the block size of `b`.

    Returns
    -------
    out : ds-array

    Raises
    ------
    NotImplementedError
        If a or b are sparse.
    """
    if a._sparse or b._sparse:
        raise NotImplementedError("Sparse ds-arrays not supported.")

    k_n_blocks = ((a.shape[0] * b._n_blocks[0]),
                  a.shape[1] * b._n_blocks[1])
    k_blocks = Array._get_out_blocks(k_n_blocks)

    # compute the kronecker product by multipliying b by each element in a.
    # The resulting array keeps the block structure of b repeated many
    # times. This is why we need to rechunk it at the end.
    offseti = 0

    for i in range(a._n_blocks[0]):
        offsetj = 0

        for j in range(a._n_blocks[1]):
            bshape_a = a._get_block_shape(i, j)

            for k in range(b._n_blocks[0]):
                for q in range(b._n_blocks[1]):
                    out_blocks = Array._get_out_blocks(bshape_a)
                    _kron(a._blocks[i][j], b._blocks[k][q], out_blocks)

                    for m in range(bshape_a[0]):
                        for n in range(bshape_a[1]):
                            bi = (offseti + m) * b._n_blocks[0] + k
                            bj = (offsetj + n) * b._n_blocks[1] + q
                            k_blocks[bi][bj] = out_blocks[m][n]

            offsetj += bshape_a[1]
        offseti += bshape_a[0]

    shape = (a.shape[0] * b.shape[0], a.shape[1] * b.shape[1])

    if not block_size:
        bsize = b._reg_shape
    else:
        bsize = block_size

    # rechunk the array unless all blocks of b are of the same size and
    # block_size is None
    if (not block_size or block_size == b._reg_shape) and (
            b.shape[0] % b._reg_shape[0] == 0 and
            b.shape[1] % b._reg_shape[1] == 0 and
            b._is_regular()):
        return Array(k_blocks, bsize, bsize, shape, False)
    else:
        out = Array._rechunk(k_blocks, shape, bsize, _kron_shape_f, b)

        for blocks in k_blocks:
            for block in blocks:
                compss_delete_object(block)

        return out


def svd(a, compute_uv=True, sort=True, copy=True, eps=1e-9):
    """ Performs singular value decomposition of a ds-array via the one-sided
    block Jacobi algorithm described in Arbenz and Slapnicar [1]_ and
    Dongarra et al. [2]_.

    Singular value decomposition is a factorization of the form A = USV',
    where U and V are unitary matrices and S is a rectangular diagonal matrix.

    Parameters
    ----------
    a : ds-array, shape=(m, n)
        Input matrix (m >= n). Needs to be partitioned in two column blocks at
        least due to the design of the block Jacobi algorithm.
    compute_uv : boolean, optional (default=True)
        Whether or not to compute u and v in addition to s.
    sort : boolean, optional (default=True)
        Whether to return sorted u, s and v. Sorting requires a significant
        amount of additional computation.
    copy : boolean, optional (default=True)
        Whether to create a copy of a or to apply transformations on a
        directly. Only valid if a is regular (i.e., top left block is of
        regular shape).
    eps : float, optional (default=1e-9)
        Tolerance for the convergence criterion.

    Returns
    -------
    u : ds-array, shape=(m, n)
        U matrix. Only returned if compute_uv is True.
    s : ds-array, shape=(1, n)
        Diagonal entries of S.
    v : ds-array, shape=(n, n)
        V matrix. Only returned if compute_uv is True.

    Raises
    ------
    ValueError
        If a has less than 2 column blocks or m < n.

    References
    ----------
    .. [1] Arbenz, P. and Slapnicar, A. (1995). An Analysis of Parallel
        Implementations of the Block-Jacobi Algorithm for Computing the SVD. In
        Proceedings of the 17th International Conference on Information
        Technology Interfaces ITI (pp. 13-16).

    .. [2] Dongarra, J., Gates, M., Haidar, A. et al. (2018). The singular
        value decomposition: Anatomy of optimizing an algorithm for extreme
        scale. In SIAM review, 60(4) (pp. 808-865).

    Examples
    --------
    >>> import numpy as np
    >>> import dislib as ds
    >>> x = ds.random_array((10, 6), (2, 2), random_state=7)
    >>> u, s, v = ds.svd(x)
    >>> u = u.collect()
    >>> s = np.diag(s.collect())
    >>> v = v.collect()
    >>> print(np.allclose(x.collect(), u @ s @ v.T))
    """
    if a._n_blocks[1] < 2:
        raise ValueError("Not enough column blocks to compute SVD.")

    if a.shape[0] < a.shape[1]:
        raise ValueError("The number of rows of the input matrix is lower "
                         "than the number of columns")

    if not a._is_regular():
        x = a.rechunk(a._reg_shape)
    elif copy:
        x = a.copy()
    else:
        x = a

    if compute_uv:
        v = identity(x.shape[1], (x._reg_shape[1], x._reg_shape[1]))

    checks = [True]

    while not _check_convergence_svd(checks):
        checks = []

        pairings = itertools.combinations_with_replacement(
            range(x._n_blocks[1]), 2
        )

        for i, j in pairings:
            if i >= j:
                continue
            coli_x = x._get_col_block(i)
            colj_x = x._get_col_block(j)

            rot, check = _compute_rotation_and_rotate(
                coli_x._blocks, colj_x._blocks, eps
            )
            checks.append(check)

            if compute_uv:
                coli_v = v._get_col_block(i)
                colj_v = v._get_col_block(j)
                _rotate(coli_v._blocks, colj_v._blocks, rot)

    s = x.norm(axis=0)

    if sort:
        sorting = _sort_s(s._blocks)

    if compute_uv:
        if sort:
            u = _compute_u_sorted(x, sorting)
            v = _sort_v(v, sorting)
        else:
            u = _compute_u(x)

        return u, s, v
    else:
        return s


def _check_convergence_svd(checks):
    for i in range(len(checks)):
        if compss_wait_on(checks[i]):
            return False

    return True


def _compute_u(a):
    u_blocks = [[] for _ in range(a._n_blocks[0])]

    for vblock in a._iterator("columns"):
        u_block = [object() for _ in range(vblock._n_blocks[0])]
        _compute_u_block(vblock._blocks, u_block)

        for i in range(len(u_block)):
            u_blocks[i].append(u_block[i])

    return Array(u_blocks, a._top_left_shape, a._reg_shape, a.shape, a._sparse)


def _compute_u_sorted(a, sorting):
    u_blocks = [[] for _ in range(a._n_blocks[1])]
    hbsize = a._reg_shape[1]

    for i, vblock in enumerate(a._iterator("columns")):
        u_block = [object() for _ in range(a._n_blocks[1])]
        _compute_u_block_sorted(vblock._blocks, i, hbsize, sorting, u_block)

        for j in range(len(u_block)):
            u_blocks[j].append(u_block[j])

    vbsize = a._reg_shape[0]
    final_blocks = Array._get_out_blocks(a._n_blocks)

    for i, u_block in enumerate(u_blocks):
        new_block = [object() for _ in range(a._n_blocks[0])]
        _merge_svd_block(u_block, i, hbsize, vbsize, sorting, new_block)

        for j in range(len(new_block)):
            final_blocks[j][i] = new_block[j]

        for elem in u_block:
            compss_delete_object(elem)

    return Array(final_blocks, a._top_left_shape, a._reg_shape, a.shape,
                 a._sparse)


def _sort_v(v, sorting):
    v_blocks = [[] for _ in range(v._n_blocks[1])]
    hbsize = v._reg_shape[1]

    for i, vblock in enumerate(v._iterator("columns")):
        out_blocks = [[] for _ in range(v._n_blocks[1])]
        _sort_v_block(vblock._blocks, i, hbsize, sorting, out_blocks)

        for j in range(len(out_blocks)):
            v_blocks[j].append(out_blocks[j])

    vbsize = v._reg_shape[0]
    final_blocks = Array._get_out_blocks(v._n_blocks)

    for i, v_block in enumerate(v_blocks):
        new_block = [object() for _ in range(v._n_blocks[0])]
        _merge_svd_block(v_block, i, hbsize, vbsize, sorting, new_block)

        for j in range(len(new_block)):
            final_blocks[j][i] = new_block[j]

        for elem in v_block:
            compss_delete_object(elem)

    return Array(final_blocks, v._top_left_shape, v._reg_shape, v.shape,
                 v._sparse)


@task(s_blocks={Type: COLLECTION_INOUT, Depth: 2}, returns=1)
def _sort_s(s_blocks):
    s = Array._merge_blocks(s_blocks)

    sorting = np.argsort(s[0])[::-1]
    s_sorted = s[0][sorting]
    bsize = s_blocks[0][0].shape[1]

    for i in range(len(s_blocks[0])):
        s_blocks[0][i] = s_sorted[i * bsize:(i + 1) * bsize].reshape(1, -1)

    return sorting


@task(a_block={Type: COLLECTION_IN, Depth: 2},
      u_block={Type: COLLECTION_OUT, Depth: 1})
def _compute_u_block(a_block, u_block):
    a_col = Array._merge_blocks(a_block)
    norm = np.linalg.norm(a_col, axis=0)

    # replace zero norm columns of a with an arbitrary unitary vector
    zero_idx = np.where(norm == 0)
    a_col[0, zero_idx] = 1
    norm[zero_idx] = 1

    u_col = a_col / norm

    block_size = a_block[0][0].shape[0]

    for i in range(len(u_block)):
        u_block[i] = u_col[i * block_size: (i + 1) * block_size]


@task(a_block={Type: COLLECTION_IN, Depth: 2},
      u_block={Type: COLLECTION_OUT, Depth: 1})
def _compute_u_block_sorted(a_block, index, bsize, sorting, u_block):
    a_col = Array._merge_blocks(a_block)
    norm = np.linalg.norm(a_col, axis=0)

    # replace zero norm columns of a with an arbitrary unitary vector
    zero_idx = np.where(norm == 0)
    a_col[0, zero_idx] = 1
    norm[zero_idx] = 1

    u_col = a_col / norm

    for i in range(len(u_block)):
        u_block[i] = []

    # place each column of U in the target block according to sorting
    for i in range(u_col.shape[1]):
        dest_i = np.where(sorting == (index * bsize + i))[0][0]
        block_i = dest_i // bsize
        u_block[block_i].append(u_col[:, i])

    for i in range(len(u_block)):
        if u_block[i]:
            u_block[i] = np.vstack(u_block[i])


@task(block={Type: COLLECTION_IN, Depth: 1},
      out_blocks={Type: COLLECTION_OUT, Depth: 1})
def _merge_svd_block(block, index, hbsize, vbsize, sorting, out_blocks):
    block = list(filter(lambda a: a != [], block))  # remove empty lists
    col = np.vstack(block).T
    local_sorting = []

    for i in range(col.shape[1]):
        dest_i = np.where(sorting == (index * hbsize + i))[0][0] % hbsize
        local_sorting.append(dest_i)

    col = col[:, local_sorting]

    for i in range(len(out_blocks)):
        out_blocks[i] = col[i * vbsize: (i + 1) * vbsize]


@task(v_block={Type: COLLECTION_IN, Depth: 2},
      out_blocks={Type: COLLECTION_OUT, Depth: 1})
def _sort_v_block(v_block, index, bsize, sorting, out_blocks):
    v_col = Array._merge_blocks(v_block)

    for i in range(v_col.shape[1]):
        dest_i = np.where(sorting == (index * bsize + i))[0][0]
        block_i = dest_i // bsize
        out_blocks[block_i].append(v_col[:, i])

    for i in range(len(out_blocks)):
        if out_blocks[i]:
            out_blocks[i] = np.vstack(out_blocks[i])


@task(coli_blocks={Type: COLLECTION_INOUT, Depth: 2},
      colj_blocks={Type: COLLECTION_INOUT, Depth: 2},
      returns=2)
def _compute_rotation_and_rotate(coli_blocks, colj_blocks, eps):
    coli = Array._merge_blocks(coli_blocks)
    colj = Array._merge_blocks(colj_blocks)

    bii = coli.T @ coli
    bjj = colj.T @ colj
    bij = coli.T @ colj

    min_shape = (min(bii.shape[0], bjj.shape[0]),
                 min(bii.shape[1], bjj.shape[1]))

    tol = eps * np.sqrt(np.sum([[bii[i][j] * bjj[i][j]
                                 for j in range(min_shape[1])]
                                for i in range(min_shape[0])]))

    if np.linalg.norm(bij) <= tol:
        return None, False
    else:
        b = np.block([[bii, bij], [bij.T, bjj]])
        j, _, _ = np.linalg.svd(b)
        _rotate(coli_blocks, colj_blocks, j)
        return j, True


@task(coli_blocks={Type: COLLECTION_INOUT, Depth: 2},
      colj_blocks={Type: COLLECTION_INOUT, Depth: 2})
def _rotate(coli_blocks, colj_blocks, j):
    if j is None:
        return

    coli = Array._merge_blocks(coli_blocks)
    colj = Array._merge_blocks(colj_blocks)

    n = coli.shape[1]
    coli_k = coli @ j[:n, :n] + colj @ j[n:, :n]
    colj_k = coli @ j[:n, n:] + colj @ j[n:, n:]

    block_size = coli_blocks[0][0].shape[0]

    for i in range(len(coli_blocks)):
        coli_blocks[i][0][:] = coli_k[i * block_size:(i + 1) * block_size][:]
        colj_blocks[i][0][:] = colj_k[i * block_size:(i + 1) * block_size][:]


def _kron_shape_f(i, j, b):
    return b._get_block_shape(i % b._n_blocks[0], j % b._n_blocks[1])


@task(out_blocks={Type: COLLECTION_OUT, Depth: 2})
def _kron(block1, block2, out_blocks):
    """ Computes the kronecker product of two blocks and returns one ndarray
    per (element-in-block1, block2) pair."""
    for i in range(block1.shape[0]):
        for j in range(block1.shape[1]):
            out_blocks[i][j] = block1[i, j] * block2
