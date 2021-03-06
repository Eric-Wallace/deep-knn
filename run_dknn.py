#!/usr/bin/env python
import os
import json
import argparse
from tqdm import tqdm
from collections import Counter
import numpy as np
import cupy as cp

import chainer
import chainer.functions as F

from nearpy import Engine
from nearpy.hashes import RandomBinaryProjectionTree
from sklearn.neighbors import KDTree

from nlp_utils import convert_seq, convert_snli_seq
from utils import setup_model

'''contains all of the code to run Deep K Nearest Neighbors
for any model'''

class DkNN:

    def __init__(self, model, lsh=False):
        self.model = model
        self.n_dknn_layers = self.model.n_dknn_layers
        self.tree_list = None
        self.label_list = None
        self._A = None
        self.lsh = lsh

    '''builds the nearest neighbor lookup data structures for all of the training
    data'''
    def build(self, train, batch_size=64, converter=convert_seq, device=0):
        train_iter = chainer.iterators.SerialIterator(
                train, batch_size, repeat=False)
        train_iter.reset()

        act_list = [[] for _ in range(self.n_dknn_layers)]
        label_list = []
        print('caching hiddens')
        n_batches = len(train) // batch_size
        for i, train_batch in enumerate(tqdm(train_iter, total=n_batches)):
            data = converter(train_batch, device=device, with_label=True)
            text = data['xs']
            labels = data['ys']

            with chainer.using_config('train', False):
                _, dknn_layers = self.model.predict(text, dknn=True)
                assert len(dknn_layers) == self.model.n_dknn_layers
            for i in range(self.n_dknn_layers):
                layer = dknn_layers[i]
                layer.to_cpu()
                act_list[i] += [x for x in layer.data]
            label_list.extend([int(x) for x in labels])
        self.act_list = act_list
        self.label_list = label_list

        if self.lsh:
            print('using Locally Sensitive Hashing for NN Search')
        else:
            print('using KDTree for NN Search')
        self.tree_list = []  # one lookup tree for each dknn layer
        for i in range(self.n_dknn_layers):
            print('building tree for layer {}'.format(i))
            if self.lsh:  # if lsh
                n_hidden = act_list[i][0].shape[0]
                rbpt = RandomBinaryProjectionTree('rbpt', 75, 75)
                tree = Engine(n_hidden, lshashes=[rbpt])

                for j, example in enumerate(tqdm(act_list[i])):
                    assert example.ndim == 1
                    assert example.shape[0] == n_hidden

                    tree.store_vector(example, j)
            else:  # if kdtree
                tree = KDTree(act_list[i])

            self.tree_list.append(tree)

    '''calibrates the model using a small heldout set'''
    def calibrate(self, data, batch_size=64, converter=convert_seq, device=0):
        data_iter = chainer.iterators.SerialIterator(
                data, batch_size, repeat=False)
        data_iter.reset()

        print('calibrating credibility')
        self._A = []
        n_batches = len(data) // batch_size
        for i, batch in enumerate(tqdm(data_iter, total=n_batches)):
            batch = converter(batch, device=device, with_label=True)
            labels = [int(x) for x in batch['ys']]
            _, knn_logits = self(batch['xs'])
            for j, _ in enumerate(batch['xs']):
                cnt_all = len(knn_logits[j])
                preds = dict(Counter(knn_logits[j]).most_common())
                cnt_y = preds.get(labels[j], 0)
                self._A.append(cnt_y / cnt_all)

    '''returns what percent of the nearest neighbors are the
    same after changing the input from x to new_x'''
    def get_neighbor_change(self, new_x, x):
        full_length_neighbors = self.get_neighbors(x)
        l10_neighbors = self.get_neighbors(new_x)
        overlap = 0.0
        for i in l10_neighbors:
            if i in full_length_neighbors:
                overlap = overlap + 1
        return overlap / len(l10_neighbors)

    '''return the distance to the nearest neighbor on the last layer'''
    def get_nearest_distance(self, xs, layer_id=-1):
        assert self.tree_list is not None
        assert self.label_list is not None

        with chainer.using_config('train', False):
            reg_logits, dknn_layers = self.model.predict(
                    xs, softmax=True, dknn=True)

        layer = dknn_layers[layer_id]
        layer.to_cpu()
        layer = [x for x in layer.data]
        neighbors, distances = [], []
        for hidden in layer:
            if self.lsh:  # use lsh
                knn = self.tree_list[layer_id].neighbours(hidden)
                for nn, dis in knn:
                    neighbors.append(nn)
                    distances.append(dis)
            else:  # use kdtree
                dis, nn = self.tree_list[layer_id].query([hidden], k=1)
                neighbors.append(nn[0][0])
                distances.append(dis[0][0])
        return distances

    ''' returns the indices of the nearest neighbors according
    to their position in the training data'''
    def get_neighbors(self, xs):
        assert self.tree_list is not None
        assert self.label_list is not None

        with chainer.using_config('train', False):
            reg_logits, dknn_layers = self.model.predict(
                    xs, softmax=True, dknn=True)

        _dknn_layers = []
        for layer in dknn_layers:
            layer.to_cpu()
            _dknn_layers.append([x for x in layer.data])
        # n_examples * n_layers
        dknn_layers = list(map(list, zip(*_dknn_layers)))

        for i, example_layers in enumerate(dknn_layers):
            # go through examples in the batch
            neighbors = []
            for layer_id, hidden in enumerate(example_layers):
                # go through layers and get neighbors for each
                if self.lsh:  # use lsh
                    knn = self.tree_list[layer_id].neighbours(hidden)
                    for nn in knn:
                        neighbors.append(nn[1])
                else:  # use kdtree
                    _, knn = self.tree_list[layer_id].query([hidden], k=75)
                    # FIXME This is the setting where you only take the last
                    # layer
                    neighbors = knn[0]
        return neighbors

    '''forward pass of model for standard inference and dknn'''
    def __call__(self, xs):
        assert self.tree_list is not None
        assert self.label_list is not None

        with chainer.using_config('train', False):
            reg_logits, dknn_layers = self.model.predict(
                    xs, softmax=True, dknn=True)

        _dknn_layers = []
        for layer in dknn_layers:
            layer.to_cpu()
            _dknn_layers.append([x for x in layer.data])
        # n_examples * n_layers
        dknn_layers = list(map(list, zip(*_dknn_layers)))

        knn_logits = []
        for i, example_layers in enumerate(dknn_layers):
            # go through examples in the batch
            neighbors = []
            for layer_id, hidden in enumerate(example_layers):
                # go through layers and get neighbors for each
                if self.lsh:  # use lsh
                    knn = self.tree_list[layer_id].neighbours(hidden)
                    for nn in knn:
                        neighbors.append(nn[1])
                else:  # use kdtree
                    _, knn = self.tree_list[layer_id].query([hidden], k=75)
                    neighbors = knn[0]

            neighbor_labels = []
            for idx in neighbors:  # for all indices, get their label
                neighbor_labels.append(self.label_list[idx])
            knn_logits .append(neighbor_labels)
        return reg_logits, knn_logits

    ''' returns credibility for a certain class ys'''
    def get_credibility(self, xs, ys, calibrated=False, use_snli=False):
        assert self.tree_list is not None
        assert self.label_list is not None

        batch_size = len(xs)
        if use_snli:
            batch_size = len(xs[0])

        _, knn_logits = self(xs)

        ys = [int(y) for y in ys]
        knn_cred = []

        for i in range(batch_size):
            cnt_all = len(knn_logits[i])
            cnts = dict(Counter(knn_logits[i]).most_common())
            p_1 = cnts.get(ys[i], 0) / cnt_all
            knn_cred.append(p_1)
        if calibrated and self._A is not None:
            for i, p_1 in enumerate(knn_cred):
                cnt_less = len([x for x in self._A if x < p_1])
                knn_cred[i] = cnt_less / len(self._A)
        return knn_cred

    '''returns confidence for standard prediction'''
    def get_regular_confidence(self, xs, ys=None, snli=False):
        reg_logits, knn_logits = self(xs)
        reg_logits = cp.asnumpy(reg_logits)
        if ys is None:
            reg_conf = np.max(reg_logits, axis=1)
        else:
            batch_size = reg_logits.shape[0]
            ys = np.array([int(y) for y in ys], dtype=np.int32)
            reg_conf = reg_logits[np.arange(batch_size), ys]
        return reg_conf

    '''predicts using normal inference and dknn. Retrieves the nearest neighbor
    hidden states, and returns the class with the highest number of nearest
    neighbors
    '''
    def predict(self, xs, calibrated=False, snli=False):
        assert self.tree_list is not None
        assert self.label_list is not None

        batch_size = len(xs)
        if snli:
            batch_size = len(xs[0])
        reg_logits, knn_logits = self(xs)

        reg_pred = F.argmax(reg_logits, 1).data.tolist()
        reg_conf = F.max(reg_logits, 1).data.tolist()

        knn_pred, knn_cred, knn_conf = [], [], []
        for i in range(batch_size):
            cnt_all = len(knn_logits[i])
            cnts = Counter(knn_logits[i]).most_common()
            label, cnt_1st = cnts[0]
            if len(cnts) > 1:
                _, cnt_2nd = cnts[1]
            else:
                cnt_2nd = 0
            p_1 = cnt_1st / cnt_all
            p_2 = cnt_2nd / cnt_all
            if calibrated and self._A is not None:
                p_1 = len([x for x in self._A if x >= p_1]) / len(self._A)
                p_2 = len([x for x in self._A if x >= p_2]) / len(self._A)
            knn_pred.append(label)
            knn_cred.append(p_1)
            knn_conf.append(1 - p_2)
        return knn_pred, knn_cred, knn_conf, reg_pred, reg_conf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', '-g', type=int, default=0,
                        help='GPU ID (negative value indicates CPU)')
    parser.add_argument('--model-setup', required=True,
                        help='Model setup dictionary.')
    parser.add_argument('--lsh', action='store_true', default=False,
                        help='If true, uses locally sensitive hashing \
                              (with k=10 NN) for NN search.')
    args = parser.parse_args()

    model, train, test, vocab, setup = setup_model(args)
    if setup['dataset'] == 'snli':
        converter = convert_snli_seq
        use_snli = True
    else:
        converter = convert_seq
        use_snli = False

    with open(os.path.join(setup['save_path'], 'calib.json')) as f:
        calibration_idx = json.load(f)

    calibration = [train[i] for i in calibration_idx]
    train = [x for i, x in enumerate(train) if i not in calibration_idx]

    '''save dknn layers for training data'''
    dknn = DkNN(model, lsh=args.lsh)
    dknn.build(train, batch_size=setup['batchsize'],
               converter=converter, device=args.gpu)

    '''calibrate the dknn credibility values'''
    dknn.calibrate(calibration, batch_size=setup['batchsize'],
                   converter=converter, device=args.gpu)

    '''run dknn on evaluation data'''
    test_iter = chainer.iterators.SerialIterator(
            test, setup['batchsize'], repeat=False)
    test_iter.reset()

    print('run dknn on evaluation data')

    total = 0
    n_reg_correct = 0
    n_knn_correct = 0
    n_batches = len(test) // setup['batchsize']
    for test_batch in tqdm(test_iter, total=n_batches):
        data = converter(test_batch, device=args.gpu, with_label=True)
        text = data['xs']
        knn_pred, knn_cred, knn_conf, reg_pred, reg_conf = dknn.predict(
                text, snli=use_snli)
        label = [int(x) for x in data['ys']]
        total += len(label)
        n_knn_correct += sum(x == y for x, y in zip(knn_pred, label))
        n_reg_correct += sum(x == y for x, y in zip(reg_pred, label))

    print('knn accuracy', n_knn_correct / total)
    print('reg accuracy', n_reg_correct / total)


if __name__ == '__main__':
    main()
