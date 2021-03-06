# distutils: language=c++
# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False
# cython: cdivision=True
# cython: embedsignature=True

from libc.stdlib cimport rand
from libc.math cimport exp
from libcpp.vector cimport vector
cimport numpy as cnp
import numpy as np
import cython

ctypedef cnp.float32_t DTYPE_t

cdef extern from "word2vec_impl.cpp":
    void update_w_eigen_impl(
        float* w_in,
        float* w_out,
        int vocab_count,
        int hiddend_dim,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr)
    void update_w_naive_impl(
        float* w_in,
        float* w_out,
        int vocab_count,
        int hiddend_dim,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr)
    void update_w_avx_impl(
        float* w_in,
        float* w_out,
        int vocab_count,
        int hiddend_dim,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr)

cdef extern from "<random>" namespace "std":
    cdef cppclass mt19937:
        mt19937()
        mt19937(unsigned int seed)

    cdef cppclass discrete_distribution[T]:
        discrete_distribution()
        discrete_distribution(vector.iterator first, vector.iterator last)
        T operator()(mt19937 gen)    

cdef (vector[int], vector[int], vector[int]) get_sg_ns_pairs(
        vector[vector[int]] texts,
        int window_size,
        int ns_count,
        vector[DTYPE_t] vocab_ns_prob):
    cdef:
        int vocab_count = vocab_ns_prob.size()
        vector[int] indices_in
        vector[int] indices_out
        vector[int] labels
        int i
        int j
        int k
        int text_size
        int curr_window_size
        int index_in
        int index_out

        mt19937 gen = mt19937(rand())
        discrete_distribution[int] negative_sampler = discrete_distribution[int](vocab_ns_prob.begin(), vocab_ns_prob.end())
        
    for i in xrange(texts.size()):
        text_size = texts[i].size()
        for j in xrange(text_size):
            index_in = texts[i][j]
            if index_in == -1:
                continue
            curr_window_size = rand() % window_size + 1
            for k in xrange(-curr_window_size, curr_window_size):
                if k == 0 or j + k < 0 or j + k >= text_size:
                    continue
                index_out = texts[i][j + k]
                if index_out == -1:
                    continue
                indices_in.push_back(index_in)
                indices_out.push_back(index_out)
                labels.push_back(1)
            
            for k in xrange(ns_count):
                index_out = negative_sampler(gen)
                indices_in.push_back(index_in)
                indices_out.push_back(index_out)
                labels.push_back(0)

    return (indices_in, indices_out, labels)

cdef void update_w_cython_impl(
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr):
    cdef:
        int i
        int j
        int label_count = labels.size()
        int hidden_dim = w_in.shape[0]
        int index_in
        int index_out
        int label
        DTYPE_t output
        DTYPE_t tmp_w_out

    for i in xrange(label_count):
        index_in = indices_in[i]
        index_out = indices_out[i]

        output = 0.0
        for j in range(hidden_dim):
            output += w_in[j, index_in] * w_out[index_out, j]
        output = 1.0 / (1.0 + exp(-output))

        label = labels[i]
        for j in range(hidden_dim):
            tmp_w_out = w_out[index_out, j]
            w_out[index_out, j] += (label - output) * w_in[j, index_in] * lr
            w_in[j, index_in] += (label - output) * tmp_w_out * lr

def update_w_naive(
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr,
    ):
    cdef:
        int vocab_count = w_in.shape[0]
        int hidden_dim = w_in.shape[1]
        vector[vector[int]] pairs
        vector[DTYPE_t] losses
        cnp.float32_t[:, ::1] w_in_cnp
        cnp.float32_t[:, ::1] w_out_cnp

    w_in_cnp = np.ascontiguousarray(w_in, dtype=np.float32)
    w_out_cnp = np.ascontiguousarray(w_out, dtype=np.float32)

    update_w_naive_impl(
        &w_in_cnp[0, 0],
        &w_out_cnp[0, 0],
        vocab_count,
        hidden_dim,
        indices_in,
        indices_out,
        labels,
        lr,
    )

def update_w_eigen(
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr,
    ):
    cdef:
        int vocab_count = w_in.shape[0]
        int hidden_dim = w_in.shape[1]
        vector[vector[int]] pairs
        vector[DTYPE_t] losses
        cnp.float32_t[:, ::1] w_in_cnp = np.ascontiguousarray(w_in, dtype=np.float32)
        cnp.float32_t[:, ::1] w_out_cnp = np.ascontiguousarray(w_out, dtype=np.float32)

    update_w_eigen_impl(
        &w_in_cnp[0, 0],
        &w_out_cnp[0, 0],
        vocab_count,
        hidden_dim,
        indices_in,
        indices_out,
        labels,
        lr,
    )

def update_w_avx(
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr,
    ):
    cdef:
        int vocab_count = w_in.shape[0]
        int hidden_dim = w_in.shape[1]
        vector[vector[int]] pairs
        vector[DTYPE_t] losses
        cnp.float32_t[:, ::1] w_in_cnp = np.ascontiguousarray(w_in, dtype=np.float32)
        cnp.float32_t[:, ::1] w_out_cnp = np.ascontiguousarray(w_out, dtype=np.float32)

    update_w_avx_impl(
        &w_in_cnp[0, 0],
        &w_out_cnp[0, 0],
        vocab_count,
        hidden_dim,
        indices_in,
        indices_out,
        labels,
        lr,
    )

def update_w_cython(
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        vector[int] indices_in,
        vector[int] indices_out,
        vector[int] labels,
        DTYPE_t lr):
    update_w_cython_impl(
        w_in,
        w_out,
        indices_in,
        indices_out,
        labels,
        lr,
    )

def get_sg_ns_grad(
        vector[vector[int]] texts,
        int window_size,
        int ns_count,
        vector[DTYPE_t] vocab_ns_prob,
        cnp.ndarray[DTYPE_t, ndim=2] w_in,
        cnp.ndarray[DTYPE_t, ndim=2] w_out,
        DTYPE_t lr):
    cdef:
        vector[int] indices_in
        vector[int] indices_out
        vector[int] labels
        cnp.ndarray[int, ndim=2] pairs
        vector[DTYPE_t] losses
    
    indices_in, indices_out, labels = get_sg_ns_pairs(texts, window_size, ns_count, vocab_ns_prob)
    update_w_cython(w_in, w_out, indices_in, indices_out, labels, lr)

    return 0
