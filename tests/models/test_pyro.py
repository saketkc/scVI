import pyro
import pyro.distributions as dist
import torch
import torch.nn as nn
from pyro.infer.autoguide import AutoDiagonalNormal
from pyro.nn import PyroModule

from scvi import _CONSTANTS
from scvi.compose import PyroBaseModuleClass
from scvi.data import synthetic_iid
from scvi.dataloaders import AnnDataLoader
from scvi.lightning import PyroTrainingPlan, Trainer


class BayesianRegression(PyroModule, PyroBaseModuleClass):
    def __init__(self, in_features, out_features):
        super().__init__()

        self._auto_guide = AutoDiagonalNormal(self)

        self.register_buffer("zero", torch.tensor(0.0))
        self.register_buffer("one", torch.tensor(1.0))
        self.register_buffer("ten", torch.tensor(10.0))

        self.linear = nn.Linear(in_features, out_features)

    @staticmethod
    def _get_fn_args_from_batch(tensor_dict):
        x = tensor_dict[_CONSTANTS.X_KEY]
        y = tensor_dict[_CONSTANTS.LABELS_KEY]

        return (x, y), {}

    def forward(self, x, y):
        sigma = pyro.sample("sigma", dist.Uniform(self.zero, self.ten))
        mean = self.linear(x).squeeze(-1)
        with pyro.plate("data", x.shape[0]):
            pyro.sample("obs", dist.Normal(mean, sigma), obs=y)
        return mean

    def guide(self, x, y):
        return self._auto_guide(x, y)


def test_pyro_bayesian_regression():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegression(adata.shape[1], 1)
    plan = PyroTrainingPlan(model)
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)


def test_pyro_bayesian_regression_jit():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegression(adata.shape[1], 1)
    # warmup guide for JIT
    for tensors in train_dl:
        args, kwargs = model._get_fn_args_from_batch(tensors)
        model.guide(*args, **kwargs)
        break
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    plan = PyroTrainingPlan(model, loss_fn=pyro.infer.JitTrace_ELBO())
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)

from scvi import _CONSTANTS
from scvi.compose import (
    BaseModuleClass,
    DecoderSCVI,
    Encoder,
    LossRecorder,
    auto_move_data,
)
from torch.distributions import constraints


class SCVIPyro(PyroModule, PyroBaseModuleClass):
    def __init__(self, n_input, n_latent):
        
        super().__init__()
        self.n_input = n_input
        self.n_latent = n_latent
        self.epsilon = 5.0e-3
        # z encoder goes from the n_input-dimensional data to an n_latent-d
        # latent space representation
        self.encoder = Encoder(
            n_input,
            n_latent,
            n_layers=1,
            n_hidden=128,
            dropout_rate=0.1,
        )
        # decoder goes from n_latent-dimensional space to n_input-d data
        self.decoder = DecoderSCVI(
            n_latent,
            n_input,
            n_layers=1,
            n_hidden=128,
        )

    @staticmethod
    def _get_fn_args_from_batch(tensor_dict):
        x = tensor_dict[_CONSTANTS.X_KEY]
        library = torch.sum(x, dim=1, keepdim=True)
        return (x, library), {}

    def forward(self, x, l):
        # register PyTorch module `decoder` with Pyro
        pyro.module("decoder", self.decoder)

        # This gene-level parameter modulates the variance of the observation distribution
        px_r = pyro.param("inverse_dispersion", 10.0 * x.new_ones(self.n_input),
                           constraint=constraints.positive)
        with pyro.plate("data", x.shape[0]):
            # setup hyperparameters for prior p(z)
            z_loc = x.new_zeros(torch.Size((x.shape[0], self.n_latent)))
            z_scale = x.new_ones(torch.Size((x.shape[0], self.n_latent)))
            # sample from prior (value will be sampled by guide when computing the ELBO)
            z = pyro.sample("latent", dist.Normal(z_loc, z_scale).to_event(1))
            # decode the latent code z
            px_scale, _, px_rate, px_dropout = self.decoder("gene", z, l)
            # build count distribution
            nb_logits = (px_rate + self.epsilon).log() - (px_r + self.epsilon).log()
            x_dist = dist.ZeroInflatedNegativeBinomial(gate_logits=px_dropout, total_count=px_r,
                                                       logits=nb_logits)
            # score against actual counts
            pyro.sample("obs", x_dist.to_event(1), obs=x)
        return None

    def guide(self, x, l):
        # define the guide (i.e. variational distribution) q(z|x)
        pyro.module("encoder", self.encoder)
        with pyro.plate("data", x.shape[0]):
            # use the encoder to get the parameters used to define q(z|x)
            x_ = torch.log(1 + x)
            z_loc, z_scale, _ = self.encoder(x_)
            # sample the latent code z
            pyro.sample("latent", dist.Normal(z_loc, z_scale).to_event(1))
        return None


def test_pyro_scvi():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = SCVIPyro(adata.n_vars, 10)
    # warmup guide for JIT
    plan = PyroTrainingPlan(model)
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)