from typing import Callable, Dict, Optional, Union, List

import torch
import torch.nn as nn
import torch.nn.functional as F

import schnetpack.properties as properties
import schnetpack.nn as snn


__all__ = ["PaiNN", "PaiNNInteraction", "PaiNNMixing"]

class EquivariantCompressionLayer(nn.Module):
    def __init__(self, num_vector_channels, compressed_channels):
        super().__init__()
        self.fc_scalar_weights = nn.Sequential(
            nn.Linear(num_vector_channels, 32),
            nn.ReLU(),
            nn.Linear(32, compressed_channels),
            nn.Softmax(dim=-1)
        )

    def forward(self, vectors):
        # vectors: [batch, num_vector_channels, 3]

        # Compute invariants (e.g., vector norms)
        scalar_invariants = torch.norm(vectors, dim=-1)  # shape: [batch, num_vector_channels]

        # Generate scalar weights equivariantly
        weights = self.fc_scalar_weights(scalar_invariants)  # [batch, compressed_channels]

        # Perform weighted pooling
        print()
        compressed_vectors = torch.einsum('bvc,bl->blc', vectors, weights)
        #torch.einsum('mij,ml->mlj', vectors, weights)  # (batch, compressed_channels, 3) <- (batch, num_vector_channels, 3) (batch, compressed_channels)
        # resulting shape: [batch, compressed_channels, 3]

        return compressed_vectors
    
class EquivariantReconstructionLayer(nn.Module):
    def __init__(self, compressed_channels, reconstructed_channels):
        super().__init__()
        self.compressed_channels = compressed_channels
        self.reconstructed_channels = reconstructed_channels
        self.fc_scalars = nn.Sequential(
            nn.Linear(compressed_channels, 32),
            nn.ReLU(),
            nn.Linear(32, reconstructed_channels * compressed_channels)
        )

    def forward(self, compressed_vectors):
        # compressed_vectors: [batch, compressed_channels, 3]

        # Compute invariants
        scalar_invariants = torch.norm(compressed_vectors, dim=-1)  # [batch, compressed_channels]
        
        # Generate scalar weights explicitly mapping between channels
        scalar_weights = self.fc_scalars(scalar_invariants).view(
            -1, self.reconstructed_channels, self.compressed_channels
        )  # [batch, reconstructed_channels, compressed_channels]
        
        # Reconstruct rank-2 tensors by tensor products
        # For simplicity, self tensor product: v ⊗ v
        rank2_tensors = torch.einsum('bci,bcj->bcij', compressed_vectors, compressed_vectors)
        # shape: [batch, compressed_channels, 3, 3]

        # Weighted sum to combine tensors
        # Explicit contraction over compressed_channels
        reconstructed_tensors = torch.einsum('brc, bcij -> brij', scalar_weights, rank2_tensors)
        # [batch, reconstructed_channels, 3, 3]

        return reconstructed_tensors

class PaiNNInteraction(nn.Module):
    r"""PaiNN interaction block for modeling equivariant interactions of atomistic systems."""

    def __init__(self, n_atom_basis: int, activation: Callable):
        """
        Args:
            n_atom_basis: number of features to describe atomic environments.
            activation: if None, no activation function is used.
        """
        super(PaiNNInteraction, self).__init__()
        self.n_atom_basis = n_atom_basis

        self.interatomic_context_net = nn.Sequential(
            snn.Dense(n_atom_basis, n_atom_basis, activation=activation),
            snn.Dense(n_atom_basis, 3 * n_atom_basis, activation=None),
        )
                
        self.compression_layer = EquivariantCompressionLayer(
            num_vector_channels=n_atom_basis*2, compressed_channels=n_atom_basis
        )
        
        self.reconstruction_layer = EquivariantReconstructionLayer(
            compressed_channels=n_atom_basis, reconstructed_channels=n_atom_basis
        )

    def forward(
        self,
        q: torch.Tensor,
        mu: torch.Tensor,
        Wij: torch.Tensor,
        dir_ij: torch.Tensor,
        idx_i: torch.Tensor,
        idx_j: torch.Tensor,
        n_atoms: int,
    ):
        """Compute interaction output.

        Args:
            q: scalar input values
            mu: vector input values
            Wij: filter
            idx_i: index of center atom i
            idx_j: index of neighbors j

        Returns:
            atom features after interaction
        """
        # inter-atomic
        x = self.interatomic_context_net(q)
        xj = x[idx_j]
        muj = mu[idx_j]
        mui = mu[idx_i]
        x = Wij * xj

        dq, dmuR, dmumu = torch.split(x, self.n_atom_basis, dim=-1)
        dq = snn.scatter_add(dq, idx_i, dim_size=n_atoms)
        compressed_vector_vj = self.compression_layer(muj)
        dmu = dmuR * dir_ij[..., None] + dmumu * compressed_vector_vj 
        dmu = snn.scatter_add(dmu, idx_i, dim_size=n_atoms)

        #q = q + dq
        #mu = mu + dmu
        compressed_vector_vi = self.compression_layer(torch.transpose(mui), 1, 2)
        q = q + dq
        mu = dmu + torch.transpose(compressed_vector_vi, 1, 2)
        dtm = self.reconstruction_layer(torch.transpose(mu, 1, 2))
        
        return q, mu, dtm


class PaiNNMixing(nn.Module):
    r"""PaiNN interaction block for mixing on atom features."""

    def __init__(self, n_atom_basis: int, activation: Callable, epsilon: float = 1e-8):
        """
        Args:
            n_atom_basis: number of features to describe atomic environments.
            activation: if None, no activation function is used.
            epsilon: stability constant added in norm to prevent numerical instabilities
        """
        super(PaiNNMixing, self).__init__()
        self.n_atom_basis = n_atom_basis

        self.intraatomic_context_net = nn.Sequential(
            snn.Dense(2 * n_atom_basis, n_atom_basis, activation=activation),
            snn.Dense(n_atom_basis, 3 * n_atom_basis, activation=None),
        )
        self.mu_channel_mix = snn.Dense(
            n_atom_basis, 2 * n_atom_basis, activation=None, bias=False
        )
        self.epsilon = epsilon

    def forward(self, q: torch.Tensor, mu: torch.Tensor, dtm: torch.Tensor):
        """Compute intraatomic mixing.

        Args:
            q: scalar input values
            mu: vector input values

        Returns:
            atom features after interaction
        """
        ## intra-atomic
        mu_mix = self.mu_channel_mix(mu)
        mu_V, mu_W = torch.split(mu_mix, self.n_atom_basis, dim=-1)
        mu_Vn = torch.sqrt(torch.sum(mu_V**2, dim=-2, keepdim=True) + self.epsilon)

        ctx = torch.cat([q, mu_Vn], dim=-1)
        x = self.intraatomic_context_net(ctx)

        dq_intra, dmu_intra, dqmu_intra = torch.split(x, self.n_atom_basis, dim=-1)
        dmu_intra = dmu_intra * mu_W

        dqmu_intra = dqmu_intra * torch.sum(mu_V * mu_W, dim=1, keepdim=True)

        #q = q + dq_intra + dqmu_intra
        #mu = mu + dmu_intra
        q = q + torch.cat([dq_intra, dqmu_intra], dim=-1)
        tensor_vector = torch.einsum('bfij,bfj->bfi', dtm, torch.transpose(q, 1, 2))  # (b, F, 3) <- (b, F, 3, 3) (b, F, 3)
        mu = mu + torch.cat([dmu_intra, torch.transpose(tensor_vector, 1, 2)], dim=-1)
        
        return q, mu


class PaiNN(nn.Module):
    """PaiNN - polarizable interaction neural network

    References:

    .. [#painn1] Schütt, Unke, Gastegger:
       Equivariant message passing for the prediction of tensorial properties and molecular spectra.
       ICML 2021, http://proceedings.mlr.press/v139/schutt21a.html

    """

    def __init__(
        self,
        n_atom_basis: int,
        n_interactions: int,
        radial_basis: nn.Module,
        cutoff_fn: Optional[Callable] = None,
        activation: Optional[Callable] = F.silu,
        shared_interactions: bool = False,
        shared_filters: bool = False,
        epsilon: float = 1e-8,
        nuclear_embedding: Optional[nn.Module] = None,
        electronic_embeddings: Optional[List] = None,
    ):
        """
        Args:
            n_atom_basis: number of features to describe atomic environments.
                This determines the size of each embedding vector; i.e. embeddings_dim.
            n_interactions: number of interaction blocks.
            radial_basis: layer for expanding interatomic distances in a basis set
            cutoff_fn: cutoff function
            activation: activation function
            shared_interactions: if True, share the weights across
                interaction blocks.
            shared_interactions: if True, share the weights across
                filter-generating networks.
            epsilon: numerical stability parameter
            nuclear_embedding: custom nuclear embedding (e.g. spk.nn.embeddings.NuclearEmbedding)
            electronic_embeddings: list of electronic embeddings. E.g. for spin and
                charge (see spk.nn.embeddings.ElectronicEmbedding)
        """
        super(PaiNN, self).__init__()

        self.n_atom_basis = n_atom_basis//2
        self.n_interactions = n_interactions
        self.cutoff_fn = cutoff_fn
        self.cutoff = cutoff_fn.cutoff
        self.radial_basis = radial_basis

        # initialize embeddings
        if nuclear_embedding is None:
            nuclear_embedding = nn.Embedding(100, n_atom_basis)
        self.embedding = nuclear_embedding
        if electronic_embeddings is None:
            electronic_embeddings = []
        electronic_embeddings = nn.ModuleList(electronic_embeddings)
        self.electronic_embeddings = electronic_embeddings

        # initialize filter layers
        self.share_filters = shared_filters
        if shared_filters:
            self.filter_net = snn.Dense(
                self.radial_basis.n_rbf, 3 * n_atom_basis, activation=None
            )
        else:
            self.filter_net = snn.Dense(
                self.radial_basis.n_rbf,
                self.n_interactions * n_atom_basis * 3,
                activation=None,
            )

        # initialize interaction blocks
        self.interactions = snn.replicate_module(
            lambda: PaiNNInteraction(
                n_atom_basis=self.n_atom_basis, activation=activation
            ),
            self.n_interactions,
            shared_interactions,
        )
        self.mixing = snn.replicate_module(
            lambda: PaiNNMixing(
                n_atom_basis=self.n_atom_basis, activation=activation, epsilon=epsilon
            ),
            self.n_interactions,
            shared_interactions,
        )

    def forward(self, inputs: Dict[str, torch.Tensor]):
        """
        Compute atomic representations/embeddings.

        Args:
            inputs: SchNetPack dictionary of input tensors.

        Returns:
            torch.Tensor: atom-wise representation.
            list of torch.Tensor: intermediate atom-wise representations, if
            return_intermediate=True was used.
        """
        # get tensors from input dictionary
        atomic_numbers = inputs[properties.Z]
        r_ij = inputs[properties.Rij]
        idx_i = inputs[properties.idx_i]
        idx_j = inputs[properties.idx_j]
        n_atoms = atomic_numbers.shape[0]

        # compute atom and pair features
        d_ij = torch.norm(r_ij, dim=1, keepdim=True)
        dir_ij = r_ij / d_ij
        phi_ij = self.radial_basis(d_ij)
        fcut = self.cutoff_fn(d_ij)

        filters = self.filter_net(phi_ij) * fcut[..., None]
        if self.share_filters:
            filter_list = [filters] * self.n_interactions
        else:
            filter_list = torch.split(filters, 3 * self.n_atom_basis, dim=-1)

        # compute initial embeddings
        q = self.embedding(atomic_numbers)
        for embedding in self.electronic_embeddings:
            q = q + embedding(q, inputs)
        q = q.unsqueeze(1)

        # compute interaction blocks and update atomic embeddings
        qs = q.shape
        mu = torch.zeros((qs[0], 3, qs[2]*2), device=q.device)
        for i, (interaction, mixing) in enumerate(zip(self.interactions, self.mixing)):
            q, mu, t = interaction(q, mu, filter_list[i], dir_ij, idx_i, idx_j, n_atoms)
            q, mu = mixing(q, mu, t)
        q = q.squeeze(1)

        # collect results
        inputs["scalar_representation"] = q
        inputs["vector_representation"] = mu

        return inputs
