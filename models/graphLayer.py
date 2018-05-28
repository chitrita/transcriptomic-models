

import sys, os
myPath = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, myPath + '/../')

import torch
import logging
import numpy as np
from torch import nn
from torch.autograd import Variable
import os
from torchvision import transforms
import sklearn
import sklearn.cluster

class PoolGraph(object):

    """
    Given x values, a adjacency graph, and a list of value to keep, return the coresponding x.
    """

    def __init__(self, adj, to_keep, please_ignore=False, type='max', on_cuda=False, **kwargs):

        self.type = type
        self.please_ignore = please_ignore
        self.adj = adj
        self.to_keep = to_keep
        self.on_cuda = on_cuda
        self.nb_nodes = self.adj.shape[0]

        logging.info("We are keeping {} elements.".format(to_keep.sum()))
        if to_keep.sum() == adj.shape[0]:
            logging.info("We are keeping all the nodes. ignoring the agregation step.")
            self.please_ignore = True

    def __call__(self, x):
        # x if of the shape (ex, node, channel)
        if self.please_ignore:
            return x

        adj = Variable(torch.FloatTensor(self.adj), requires_grad=False)
        to_keep = Variable(torch.FloatTensor(self.to_keep.astype(float)), requires_grad=False)
        if self.on_cuda:
            adj = adj.cuda()
            to_keep = to_keep.cuda()

        x = x.permute(0, 2, 1).contiguous()  # put in ex, channel, node
        x_shape = x.size()

        if self.type == 'max':
            max_value = (x.view(-1, x.size(-1), 1) * adj).max(dim=1)[0]
        elif self.type == 'mean':
            max_value = (x.view(-1, x.size(-1), 1) * adj).mean(dim=1)
        elif self.type == 'strip':
            max_value = x.view(-1, x.size(-1))
        else:
            raise ValueError()

        retn = max_value * to_keep  # Zero out The one that we don't care about.
        retn = retn.view(x_shape).permute(0, 2, 1).contiguous()  # put back in ex, node, channel
        return retn


class AggregationGraph(object):

    """
    Master Agregator. Will return the agregator function and the adj for each layer of the network.
    """

    def __init__(self, adj, nb_layer, adj_transform=None, on_cuda=False, cluster_type=None, **kwargs):

        self.nb_layer = nb_layer
        self.adj = adj
        self.on_cuda = on_cuda
        self.adj_transform = adj_transform
        self.cluster_type = cluster_type

        # Build the hierarchy of clusters.
        self.init_cluster()  # Compute all the adjs and to_keep variables.

        # Build the aggregate function
        self.aggregates = []
        for adj, to_keep in zip(self.aggregate_adjs, self.to_keeps):
            aggregate_adj = PoolGraph(adj=adj, to_keep=to_keep, on_cuda=on_cuda)
            self.aggregates.append(aggregate_adj)

    def init_cluster(self):
        # Cluster multi-scale everything
        nb_nodes = self.adj.shape[0]
        to_keep = np.ones((nb_nodes,))

        all_to_keep = []  # At each agregation, which node to keep.
        all_aggregate_adjs = []  # At each agregation, which node are connected to whom.
        all_transformed_adj = []  # At each layer, the transformed adj (normalized, etc.

        current_adj = self.adj.copy()

        # For each layer, build the adjs and the nodes to keep.
        for no_layer in range(self.nb_layer):

            if self.adj_transform:  # Transform the adj if necessary.
                current_adj = self.adj_transform(no_layer)(current_adj)

            all_transformed_adj.append(current_adj)

            to_keep, adj = self.cluster_specific_layer(to_keep, no_layer, np.array(current_adj))
            all_to_keep.append(to_keep)
            all_aggregate_adjs.append(adj)

            current_adj = adj

        self.to_keeps = all_to_keep
        self.aggregate_adjs = all_aggregate_adjs
        self.adjs = all_transformed_adj

    def get_nodes_cluster(self, last_to_keep, layer_id, adj):
        # TODO: add other kind of clustering (i.e. random, grid, etc.)

        nb_nodes = adj.shape[0]
        ids = range(adj.shape[0])

        if self.cluster_type == 'hierarchy':
            n_clusters = nb_nodes / (2 ** (layer_id + 1))
            # For a specific layer, return the ids. The merging and stuff's gonna be compute later.
            self.clustering = sklearn.cluster.AgglomerativeClustering(n_clusters=n_clusters, affinity='euclidean',
                                                                      memory='testing123_123', connectivity=(adj > 0.).astype(int),
                                                                      compute_full_tree='auto', linkage='ward')
            ids = self.clustering.fit_predict(self.adj)  # all nodes has a cluster.
        elif self.cluster_type is None or self.cluster_type == 'ignore':
            pass
        elif self.cluster_type == 'grid':
            grid_size = int(np.sqrt(nb_nodes))
            ids = [1 if (i % grid_size) == 0 else 0 for i in range(nb_nodes)]
        else:
            raise ValueError('Cluster type {} unknown.'.format(self.cluster_type))

        return ids

    def cluster_specific_layer(self, last_to_keep, n_clusters, adj):
        # A cluster for a specific scale.
        ids = self.get_nodes_cluster(last_to_keep, n_clusters, adj)
        n_clusters = len(set(ids))

        clusters = set([])
        to_keep = np.zeros((adj.shape[0],))
        cluster_adj = np.zeros((n_clusters, self.adj.shape[0]))

        for i, cluster in enumerate(ids):
            if last_to_keep[i] == 1.:  # To keep a node, it had to be a centroid of a previous layer. Otherwise it might not work.
                if cluster not in clusters:
                    clusters.add(cluster)
                    to_keep[i] = 1.

            cluster_adj[cluster] += adj[i]  # The centroid is the merged of all the adj of all the nodes inside it.

        new_adj = np.zeros((adj.shape[0], adj.shape[0]))  # rewrite the adj matrix.
        for i, cluster in enumerate(ids):
            new_adj[i] += (cluster_adj[cluster] > 0.).astype(int)

        return to_keep, new_adj

    def get_aggregate(self, layer_id):
        return self.aggregates[layer_id]

    def get_adj(self, adj, layer_id):

        # To be a bit more consistant we also pass the adj.
        # Here we don't use it.
        return self.adjs[layer_id]


class SelfConnection(object):

    """
    Add (or not) the self connection to the network.
    """

    def __init__(self, add_self_connection, please_ignore, **kwargs):
        self.add_self_connection = add_self_connection
        self.please_ignore = please_ignore

    def __call__(self, adj):

        logging.info("Adding self connection!")

        if self.add_self_connection:
            np.fill_diagonal(adj, 1.)
        else:
            np.fill_diagonal(adj, 0.)

        return adj


class ApprNormalizeLaplacian(object):
    """
    Approximate a normalized Laplacian based on https://arxiv.org/pdf/1609.02907.pdf

    Args:
        processed_path (string): Where to save the processed normalized adjency matrix.
        overwrite (bool): If we want to overwrite the saved processed data.

    """

    # TODO: add unittests
    def __init__(self, processed_dir='/Tmp/',
                 processed_file=None, unique_id=None, overwrite=False, **kwargs):

        import getpass

        self.processed_dir = os.path.join(processed_dir, "ApprNormalizeLaplacian-" + str(getpass.getuser()))
        self.processed_file = processed_file
        self.overwrite = overwrite
        self.unique_id = unique_id

    def __call__(self, adj):

        adj = np.array(adj)
        adj_hash = str(hash(str(adj))) + str(adj.shape)
        processed_path = None
        if self.processed_dir and self.processed_file:
            processed_path = os.path.join(self.processed_dir, self.processed_file)

            if not os.path.exists(processed_path):
                os.makedirs(processed_path)

            processed_path = processed_path + adj_hash + '_{}.npy'.format(self.unique_id)

            if not self.overwrite and os.path.exists(processed_path):
                logging.info("returning a saved transformation.")
                return np.load(processed_path)

        logging.info("Doing the approximation...")

        # Fill the diagonal
        np.fill_diagonal(adj, 1.)  # TODO: Hummm, think it's a 0.

        D = adj.sum(axis=1)
        D_inv = np.diag(1. / np.sqrt(D))
        norm_transform = D_inv.dot(adj).dot(D_inv)

        logging.info("Done!")

        # saving the processed approximation
        if processed_path:
            logging.info("Saving the approximation in {}".format(processed_path))
            np.save(processed_path, norm_transform)
            logging.info("Done!")

        return norm_transform


class AugmentGraphConnectivity(object):

    def __init__(self, kernel_size=1, please_ignore=False, **kwargs):

        self.kernel_size = kernel_size
        self.please_ignore = please_ignore

    def __call__(self, adj):

        """
        Augment the connectivity of the nodes in the graph.
        :param adj: The adj matrix
        :param stride: The stride of the pooling. Akin to CNN.
        :param kernel_size: The size of the neibourhood. Same thing as in CNN.
        :param please_ignore: We are not doing pruning, this option is to make things more consistant.
        :return:
        """

        kernel_size = self.kernel_size
        please_ignore = self.please_ignore

        # We don't do pruning.
        if please_ignore:
            return adj
        else:
            print "Pruning the graph."

        # TODO: do it by order of degree, so that we have some garantee
        degrees = adj.sum(axis=0)
        degrees = np.argsort(degrees)[::-1]

        current_adj = adj
        # We link all the neighbour of the neighbour (times kernel_size) to our node.
        for i in range(kernel_size):
            current_adj = current_adj.dot(current_adj.T)

        frozen_adj = current_adj.copy()

        new_adj = (frozen_adj > 0).astype(float)

        return new_adj


class GraphLayer(nn.Module):
    def __init__(self, adj, in_dim=1, channels=1, on_cuda=False, id_layer=None,
                 transform_adj=None, aggregate_adj=None):
        super(GraphLayer, self).__init__()
        self.my_layers = []
        self.on_cuda = on_cuda
        self.nb_nodes = adj.shape[0]
        self.in_dim = in_dim
        self.channels = channels
        self.id_layer = id_layer
        self.transform_adj = transform_adj  # How to transform the adj matrix.
        self.aggregate_adj = aggregate_adj

        if self.transform_adj is not None:
            logging.info("Transforming the adj matrix")
            adj = self.transform_adj(adj, id_layer)
        self.adj = adj

        if self.aggregate_adj is not None:
            self.aggregate_adj = self.aggregate_adj(id_layer)
        #self.to_keep = self.aggregate_adj.to_keep

        self.init_params()

    def init_params(self):
        raise NotImplementedError()

    def forward(self, x):
        raise NotImplementedError()


class SparseMM(torch.autograd.Function):
    """
    Sparse x dense matrix multiplication with autograd support.
    Implementation by Soumith Chintala:
    https://discuss.pytorch.org/t/
    does-pytorch-support-autograd-on-sparse-matrix/6156/7
    From: https://github.com/tkipf/pygcn/blob/master/pygcn/layers.py
    """

    def __init__(self, sparse):
        super(SparseMM, self).__init__()
        self.sparse = sparse

    def forward(self, dense):
        return torch.mm(self.sparse, dense)

    def backward(self, grad_output):
        grad_input = None
        if self.needs_input_grad[0]:
            grad_input = torch.mm(self.sparse.t(), grad_output)
        return grad_input


class CGNLayer(GraphLayer):

    def init_params(self):
        self.edges = torch.LongTensor(np.array(np.where(self.adj)))  # The list of edges
        flat_adj = self.adj.flatten()[np.where(self.adj.flatten())]  # get the value
        flat_adj = torch.FloatTensor(flat_adj)

        # Constructing a sparse matrix
        logging.info("Constructing the sparse matrix...")
        sparse_adj = torch.sparse.FloatTensor(self.edges, flat_adj, torch.Size([self.nb_nodes, self.nb_nodes]))  # .to_dense()
        self.register_buffer('sparse_adj', sparse_adj)
        self.linear = nn.Conv1d(self.in_dim, self.channels/2, 1, bias=True)  # something to be done with the stride?
        self.eye_linear = nn.Conv1d(self.in_dim, self.channels/2, 1, bias=True)

    def _adj_mul(self, x, D):
        nb_examples, nb_channels, nb_nodes = x.size()
        x = x.view(-1, nb_nodes)

        # Needs this hack to work: https://discuss.pytorch.org/t/does-pytorch-support-autograd-on-sparse-matrix/6156/7
        #x = D.mm(x.t()).t()
        x = SparseMM(D)(x.t()).t()

        x = x.contiguous().view(nb_examples, nb_channels, nb_nodes)
        return x

    def forward(self, x):

        x = x.permute(0, 2, 1).contiguous()  # from ex, node, ch, -> ex, ch, node

        adj = Variable(self.sparse_adj, requires_grad=False)

        eye_x = self.eye_linear(x)

        x = self._adj_mul(x, adj)  # + old_x# local average

        x = torch.cat([self.linear(x), eye_x], dim=1)  # + old_x# conv

        x = x.permute(0, 2, 1).contiguous()  # from ex, ch, node -> ex, node, ch

        # We can do max pooling and stuff, if we want.
        if self.aggregate_adj:
            x = self.aggregate_adj(x)

        return x


class LCGLayer(GraphLayer):

    def init_params(self):
        logging.info("Constructing the network...")
        self.max_edges = sorted((self.adj > 0.).sum(0))[-1]

        logging.info("Each node will have {} edges.".format(self.max_edges))

        # Get the list of all the edges. All the first index is 0, we fix that later
        edges_np = [np.asarray(np.where(self.adj[i:i + 1] > 0.)).T for i in range(len(self.adj))]

        # pad the edges, so they all nodes have the same number of edges. help to automate everything.
        edges_np = [np.concatenate([x, [[0, self.nb_nodes]] * (self.max_edges - len(x))]) if len(x) < self.max_edges
                    else x[:self.max_edges] if len(x) > self.max_edges  # Some Nodes have too many connection!
                    else x
                    for i, x in enumerate(edges_np)]

        # fix the index that was all 0.
        for i in range(len(edges_np)):
            edges_np[i][:, 0] = i

        edges_np = np.array(edges_np).reshape(-1, 2)
        edges_np = edges_np[:, 1:2]

        self.edges = torch.LongTensor(edges_np)
        self.super_edges = torch.cat([self.edges] * self.channels)

        # We have one set of parameters per input dim. might be slow, but for now we will do with that.
        self.my_weights = [nn.Parameter(torch.rand(self.edges.shape[0], self.channels), requires_grad=True) for _ in  # TODO: to glorot
                           range(self.in_dim)]
        self.my_weights = nn.ParameterList(self.my_weights)

    def GraphConv(self, x, edges, batch_size, weights):

        edges = edges.contiguous().view(-1)
        useless_node = Variable(torch.zeros(x.size(0), 1, x.size(2)))

        if self.on_cuda:
            edges = edges.cuda()
            weights = weights.cuda()
            useless_node = useless_node.cuda()

        x = torch.cat([x, useless_node], 1)  # add a random filler node
        tocompute = torch.index_select(x, 1, Variable(edges)).view(batch_size, -1, weights.size(-1))

        conv = tocompute * weights
        conv = conv.view(-1, self.nb_nodes, self.max_edges, weights.size(-1)).sum(2)
        return conv

    def forward(self, x):

        nb_examples, nb_nodes, nb_channels = x.size()
        edges = Variable(self.super_edges, requires_grad=False)

        if self.on_cuda:
            edges = edges.cuda()

        # DO all the input channel and sum them.
        x = sum([self.GraphConv(x[:, :, i].unsqueeze(-1), edges.data, nb_examples, self.my_weights[i]) for i in range(self.in_dim)])

        # We can do max pooling and stuff, if we want.
        if self.aggregate_adj:
            x = self.aggregate_adj(x)

        return x


# spectral graph conv
class SGCLayer(GraphLayer):

    def init_params(self):
        if self.channels != 1:
            logging.info("Setting Channels to 1 on SGCLayer, only number of channels supported")
        self.channels = 1  # Other number of channels not suported.

        logging.info("Constructing the eigenvectors...")

        D = np.diag(self.adj.sum(axis=1))
        self.L = D - self.adj
        self.L = torch.FloatTensor(self.L)
        self.g, self.V = torch.eig(self.L, eigenvectors=True)
        self.F = nn.Parameter(torch.rand(self.nb_nodes, self.nb_nodes), requires_grad=True)

    def forward(self, x):
        V = self.V
        if self.on_cuda:
            V = self.V.cuda()

        Vx = torch.matmul(torch.transpose(Variable(V), 0, 1), x)
        FVx = torch.matmul(self.F, Vx)
        VFVx = torch.matmul(Variable(V), FVx)
        x = VFVx

        # We can do max pooling and stuff, if we want.
        if self.aggregate_adj:
            x = self.aggregate_adj(x)

        return x


def get_transform(opt, adj):

    """
    Return a list of transform that can be applied to the adjacency matrix.
    :param opt: the options
    :return: The list of transform.
    """

    adj_transform = []
    if opt.add_self:
        logging.info("Adding self connection to the graph...")
        adj_transform += [lambda layer_id: SelfConnection(opt.add_self, please_ignore=False)]  # Add a self connection.

    if opt.add_connectivity:
        logging.info("Adding the connectivity after each layer...")
        adj_transform += [lambda layer_id: AugmentGraphConnectivity(please_ignore=layer_id == 0)]  # Augmenting the connectivity of each layer.

    if opt.norm_adj:
        logging.info("Normalizing the graph...")
        adj_transform += [lambda layer_id: ApprNormalizeLaplacian(processed_file=opt.graph)]  # Normalize the graph

    #if opt.pool_graph == "ignore":
#        def get_aggregate(self, layer_id):
#            return self.aggregates[layer_id]
#        def get_adj(self, adj, layer_id):
#            return self.adjs[layer_id]

    # Our adj transform method.
    adj_transform = transforms.Compose(adj_transform)
    agregator = AggregationGraph(adj, opt.num_layer, adj_transform=adj_transform, on_cuda=opt.cuda, cluster_type=opt.pool_graph)  # TODO: pooling and stuff

    # I don't want the code to be too class dependant, so I'll two a functions instead.
    # 1. A function to get the adj matrix
    # 2. A agregation fonction.
    # For now The only parameter if takes in is the layer id.
    get_adj = lambda adj, layer_id: agregator.get_adj(adj, layer_id)
    get_aggregate = lambda layer_id: agregator.get_aggregate(layer_id)
    return get_adj, get_aggregate
