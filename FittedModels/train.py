import torch
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from FittedModels.Models.base import BaseLearntDistribution
from FittedModels.Utils.plotting_utils import plot_samples

Notebook = False
if Notebook:
    from tqdm.notebook import tqdm
else:
    from tqdm import tqdm


class LearntDistributionManager:
    def __init__(self, target_distribution, fitted_model, importance_sampler,
                 loss_type="DReG", alpha=2, lr=1e-3, weight_decay=1e-6, k=None, use_GPU=True, optimizer="Adamax",
                 annealing=False):
        if use_GPU is True:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = "cpu"
        self.importance_sampler = importance_sampler
        self.learnt_sampling_dist: BaseLearntDistribution
        self.learnt_sampling_dist = fitted_model.to(self.device)
        self.target_dist = target_distribution.to(self.device)
        self.loss_type = loss_type
        if optimizer == "Adam":
            torch_optimizer = torch.optim.Adam
        elif optimizer == "Adamax":
                torch_optimizer = torch.optim.Adamax
        else:
            raise Exception(f"optimizer type: '{optimizer}' not recognised")
        self.optimizer = torch_optimizer(self.learnt_sampling_dist.parameters(), lr=lr, weight_decay=weight_decay)
        self.setup_loss(loss_type=loss_type, alpha=alpha, k=k, annealing=annealing)



    def setup_loss(self, loss_type, alpha=2, k=None, new_lr=None, annealing=False):
        self.annealing = annealing
        self.k = k # if DReG then k is number of samples that going inside the log sum, if none then put all of them inside
        if loss_type == "kl":
            self.loss = self.KL_loss
            self.alpha = 1
        elif loss_type == "DReG":
            self.loss = self.dreg_alpha_divergence_loss
            self.alpha = alpha  # alpha for alpha-divergence
            self.alpha_one_minus_alpha_sign = torch.sign(torch.tensor(self.alpha * (1 - self.alpha)))
        elif loss_type == "DReG_kl":
            self.loss = self.dreg_kl_loss
            self.alpha = 1
        elif loss_type == "alpha_MC":  # this does terribly
            self.loss = self.alpha_MC_loss
            self.alpha = alpha  # alpha for alpha-divergence
            self.alpha_one_minus_alpha_sign = torch.sign(torch.tensor(self.alpha * (1 - self.alpha)))
        else:
            raise Exception("loss_type incorrectly specified")
        if new_lr is not None:
            self.optimizer.param_groups[0]["lr"] = new_lr

    def train(self, epochs=100, batch_size=256, extra_info=True,
              clip_grad_norm=False, max_grad_norm=1, clip_grad_max=False,
              max_grad_value=0.5,
              KPI_batch_size=int(1e4), intermediate_plots=False,
              plotting_func=plot_samples):
        """
        :param epochs:
        :param batch_size:
        :param extra_info: print MC estimates of divergences, and importance sampling info
        :param clip_grad_norm: max norm gradient clipping
        :param max_grad_norm: for gradient clipping
        :param KPI_batch_size:  n_samples used for MC estimates of divergences and importance sampling info
        :param intermediate_plots: plot samples throughout training
        :return: dictionary of training history
        """

        if "DReG" in self.loss_type and self.k is None:
            self.k = batch_size
        self.total_epochs = epochs  # we need this for annealing if we use it

        epoch_per_print = min(max(int(epochs / 100), 1), 100)  # max 100 epoch, min 1 epoch
        epoch_per_save = max(int(epochs / 100), 1)
        if intermediate_plots is True:
            epoch_per_plot = max(int(epochs / 10), 1)
        history = {"loss": [],
                   "log_p_x": [],
                   "log_q_x": []}
        if extra_info is True:
            history.update({
               "kl": [],
               "alpha_2_divergence": [],
               "importance_weights_var": [],
               "normalised_importance_weights_var": [],
                "effective_sample_size": []})
            if hasattr(self.target_dist, "sample"):
                history.update({"alpha_2_divergence_over_p": []})

        pbar = tqdm(range(epochs), position=0, leave=True)
        for self.current_epoch in pbar:
            self.optimizer.zero_grad()
            x_samples, log_q_x = self.learnt_sampling_dist(batch_size)
            log_p_x = self.target_dist.log_prob(x_samples)
            loss = self.loss(x_samples, log_q_x, log_p_x)
            if torch.isnan(loss) or torch.isinf(loss):
                raise Exception("Nan/Inf loss encountered")
            loss.backward()
            if clip_grad_max is True:
                torch.nn.utils.clip_grad_value_(self.learnt_sampling_dist.parameters(), max_grad_value)
            if clip_grad_norm is True:
                grad_norm = torch.nn.utils.clip_grad_norm_(self.learnt_sampling_dist.parameters(), max_grad_norm)
            self.optimizer.step()
            history["loss"].append(loss.item())
            history["log_p_x"].append(torch.mean(log_p_x).item())
            history["log_q_x"].append(torch.mean(log_q_x).item())
            if self.current_epoch % epoch_per_print == 0 or self.current_epoch == epochs:
                pbar.set_description(f"loss: {history['loss'][-1]}, mean log p_x {torch.mean(log_p_x)}")
            if self.current_epoch % epoch_per_save == 0 or self.current_epoch == epochs:
                history["kl"].append(self.kl_MC_estimate(KPI_batch_size))
                history["alpha_2_divergence"].append(self.alpha_divergence_MC_estimate(KPI_batch_size))
                if hasattr(self.target_dist, "sample"):  # check if sample func exists
                    try:
                        history["alpha_2_divergence_over_p"].append(self.alpha_divergence_over_p_MC_estimate(KPI_batch_size))
                    except:
                        print("Couldn't calculate alpha divergence over p")
                importance_weights_var, normalised_importance_weights_var, ESS = self.importance_weights_key_info(KPI_batch_size)
                history["importance_weights_var"].append(importance_weights_var)
                history["normalised_importance_weights_var"].append(normalised_importance_weights_var)
                history["effective_sample_size"].append(ESS)
            if intermediate_plots:
                if self.current_epoch % epoch_per_plot == 0:
                    plotting_func(self, n_samples=1000)
                    plt.show()
        return history

    def to(self, device):
        """device is cuda or cpu"""
        self.device = device
        self.learnt_sampling_dist.to(self.device)
        self.target_dist.to(self.device)
        if hasattr(self, "fixed_learnt_sampling_dist"):
            self.fixed_learnt_sampling_dist.to(self.device)

    def KL_loss(self, x_samples_not_used, log_q_x, log_p_x):
        kl = log_q_x - log_p_x*self.beta
        kl_loss = torch.mean(kl)
        return kl_loss

    @property
    def beta(self):
        if self.annealing is False:
            return 1.0
        else:
            annealing_period = int(self.total_epochs/2)  #  anneal during first half of training
            return min(1.0, 0.001 + self.current_epoch/annealing_period)

    def dreg_alpha_divergence_loss(self, x_samples, log_q_x_not_used, log_p_x):
        self.learnt_sampling_dist.set_requires_grad(False)
        log_q_x = self.learnt_sampling_dist.log_prob(x_samples)
        self.learnt_sampling_dist.set_requires_grad(True)
        log_w = log_p_x - log_q_x
        outside_dim = log_q_x.shape[0]/self.k  # this is like a batch dimension that we average DReG estimation over
        assert outside_dim % 1 == 0  # always make k & n_samples work together nicely for averaging
        outside_dim = int(outside_dim)
        log_w = log_w.reshape((outside_dim, self.k))
        with torch.no_grad():
            w_alpha_normalised_alpha = F.softmax(self.alpha*log_w, dim=-1)
        DreG_for_each_batch_dim = - self.alpha_one_minus_alpha_sign * \
                    torch.sum(((1 - self.alpha) * w_alpha_normalised_alpha + self.alpha * w_alpha_normalised_alpha**2)
                              * log_w, dim=-1)
        dreg_loss = torch.mean(DreG_for_each_batch_dim)
        return dreg_loss

    def dreg_alpha_divergence_loss_prior_training(self, x_samples, log_q_x_not_used, log_p_x):
        self.learnt_sampling_dist.set_requires_grad(False)
        log_q_x = self.learnt_sampling_dist.log_prob(x_samples)
        self.learnt_sampling_dist.set_requires_grad(True)
        # flow params must remain false when we are training only the prior
        self.learnt_sampling_dist.set_flow_requires_grad(False)
        log_w = log_p_x - log_q_x
        outside_dim = log_q_x.shape[0]/self.k  # this is like a batch dimension that we average DReG estimation over
        assert outside_dim % 1 == 0  # always make k & n_samples work together nicely for averaging
        outside_dim = int(outside_dim)
        log_w = log_w.reshape((outside_dim, self.k))
        with torch.no_grad():
            w_alpha_normalised_alpha = F.softmax(self.alpha*log_w, dim=-1)
        DreG_for_each_batch_dim = - self.alpha_one_minus_alpha_sign * \
                    torch.sum(((1 - self.alpha) * w_alpha_normalised_alpha + self.alpha * w_alpha_normalised_alpha**2)
                              * log_w, dim=-1)
        dreg_loss = torch.mean(DreG_for_each_batch_dim)
        return dreg_loss


    def dreg_kl_loss(self, x_samples, log_q_x_not_used, log_p_x):
        self.learnt_sampling_dist.set_requires_grad(False)
        log_q_x = self.learnt_sampling_dist.log_prob(x_samples)
        self.learnt_sampling_dist.set_requires_grad(True)
        log_w = log_p_x - log_q_x
        outside_dim = log_q_x.shape[0]/self.k  # this is like a batch dimension that we average DReG estimation over
        assert outside_dim % 1 == 0  # always make k & n_samples work together nicely for averaging
        outside_dim = int(outside_dim)
        log_w = log_w.reshape((outside_dim, self.k))
        with torch.no_grad():
            w_normalised_squared = F.softmax(log_w, dim=-1)**2
        DreG_for_each_batch_dim = - torch.sum(w_normalised_squared * log_w, dim=-1)
        dreg_loss = torch.mean(DreG_for_each_batch_dim)
        return dreg_loss

    def train_prior(self, epochs, batch_size, loss_type="DReG", lr=0.005):
        loss_kwargs = {"loss_type": self.loss_type,
                       "alpha": self.alpha,
                       "k": self.k,
                       "new_lr": self.optimizer.param_groups[0]["lr"],
                       "annealing": self.annealing}  # save so we can reset this after prior training
        self.loss_type = loss_type + "prior"
        # first let's train just the prior
        if loss_type == "DReG":
            self.loss = self.dreg_alpha_divergence_loss_prior_training
            self.alpha = 2  # alpha for alpha-divergence
            self.alpha_one_minus_alpha_sign = torch.sign(torch.tensor(self.alpha * (1 - self.alpha)))
        self.optimizer.param_groups[0]["lr"] = lr
        self.learnt_sampling_dist.set_flow_requires_grad(False)
        history = self.train(epochs, batch_size=int(batch_size))
        self.learnt_sampling_dist.set_flow_requires_grad(True)
        self.setup_loss(**loss_kwargs)
        return history

    def importance_weights_key_info(self, batch_size=1000):
        x_samples, log_q_x = self.learnt_sampling_dist(batch_size)
        log_p_x = self.target_dist.log_prob(x_samples)
        # variance in unnormalised weights
        weights = torch.exp(log_p_x - log_q_x)
        normalised_weights = torch.softmax(log_p_x - log_q_x, dim=-1)
        ESS = self.importance_sampler.effective_sample_size(normalised_weights)/batch_size
        return torch.var(weights).item(), torch.var(normalised_weights).item(), ESS.item()

    def get_gradients(self, n_batches=100, batch_size=100):
        grads = []
        for i in range(n_batches):
            self.optimizer.zero_grad()
            x_samples, log_q_x = self.learnt_sampling_dist(batch_size)
            log_p_x = self.target_dist.log_prob(x_samples)
            loss = self.loss(x_samples, log_q_x, log_p_x)
            self.learnt_sampling_dist.flow_blocks[0].AutoregressiveNN.FinalLayer.layer_to_m.weight.register_hook(
                lambda grad: grads.append(grad.detach())
            )
            loss.backward()
        grads = torch.stack(grads)
        return grads

    def alpha_MC_loss(self, x_samples_not_used, log_q_x, log_p_x):
        alpha_div = -self.alpha_one_minus_alpha_sign*self.alpha*(log_p_x - log_q_x)
        MC_loss = torch.mean(alpha_div)
        return MC_loss

    def kl_MC_estimate(self, batch_size=1000):
        x_samples, log_q_x = self.learnt_sampling_dist(batch_size)
        log_p_x = self.target_dist.log_prob(x_samples)
        kl = log_q_x - log_p_x
        kl_loss = torch.mean(kl)
        return kl_loss.item()


    def alpha_divergence_MC_estimate(self, batch_size=1000, alpha=2):
        alpha_one_minus_alpha_sign = torch.sign(torch.tensor(alpha * (1 - alpha)))
        x_samples, log_q_x = self.learnt_sampling_dist(batch_size)
        log_p_x = self.target_dist.log_prob(x_samples)
        N = torch.tensor(log_p_x.shape[0])
        log_alpha_divergence = -alpha_one_minus_alpha_sign * \
                               (torch.logsumexp(alpha*(log_p_x - log_q_x), dim=-1) - torch.log(N))
        return log_alpha_divergence.item()

    def alpha_divergence_over_p_MC_estimate(self, batch_size=1000, alpha=2):
        alpha_one_minus_alpha_sign = torch.sign(torch.tensor(alpha * (1 - alpha)))
        x_samples = self.target_dist.sample((batch_size,))
        log_q_x = self.learnt_sampling_dist.log_prob(x_samples)
        log_p_x = self.target_dist.log_prob(x_samples)
        N = torch.tensor(log_p_x.shape[0])
        log_alpha_divergence = -alpha_one_minus_alpha_sign * \
                               (torch.logsumexp((alpha - 1) * (log_p_x - log_q_x), dim=-1) - torch.log(N))
        return log_alpha_divergence.item()


    @torch.no_grad()
    def estimate_expectation(self, n_samples, expectation_function, device="cpu"):
        # run on cpu to handle big batch size, we could also use our batchify function here instead
        original_device = self.device
        self.to(device)
        importance_sampler = self.importance_sampler(self.learnt_sampling_dist, self.target_dist)
        expectation, expectation_info = importance_sampler.calculate_expectation(n_samples, expectation_function)
        self.to(original_device)
        return expectation, expectation_info

    def effective_sample_size(self, normalised_sampling_weights):
        return self.importance_sampler.effective_sample_size(normalised_sampling_weights)





if __name__ == '__main__':
    import torch
    import matplotlib.pyplot as plt
    from FittedModels.Utils.plotting_utils import plot_distributions
    torch.manual_seed(0)
    from ImportanceSampling.VanillaImportanceSampler import VanillaImportanceSampling
    from TargetDistributions.Guassian_FullCov import Guassian_FullCov
    from FittedModels.Models.DiagonalGaussian import DiagonalGaussian
    from FittedModels.Utils.plotting_utils import plot_distributions
    from Utils.numerical_utils import quadratic_function as expectation_function
    epochs = 500
    dim = 2
    target = Guassian_FullCov(dim=dim)
    learnt_sampler = DiagonalGaussian(dim=dim)
    tester = LearntDistributionManager(target, learnt_sampler, VanillaImportanceSampling, loss_type="DReG",
                                       weight_decay=1e-5)
    tester.train_prior(epochs=100, batch_size=100)
    
    if dim == 2:
        fig_before = fig_before_train = plot_distributions(tester)
    expectation_before, sampling_weights_before = tester.estimate_expectation(int(1e5),
                                                                expectation_function=expectation_function)
    plt.show()

    history = tester.train(epochs, intermediate_plots=True)
    expectation, expectation_info = tester.estimate_expectation(int(1e5),
                                                                expectation_function=expectation_function)


    print(f"estimate before training is {expectation_before} \n"
          f"estimate after training is {expectation}")

    if dim == 2:
        fig_after_train = plot_distributions(tester)
        plt.show()

    figure, axs = plt.subplots(len(history), 1, figsize=(6, 10))
    for i, key in enumerate(history):
        axs[i].plot(history[key])
        axs[i].set_title(key)
        if key == "alpha_divergence":
            axs[i].set_yscale("log")
    plt.show()



