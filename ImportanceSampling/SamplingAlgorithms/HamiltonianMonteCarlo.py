import torch
import torch.nn as nn
from ImportanceSampling.SamplingAlgorithms.base import BaseTransitionModel
from NormalisingFlow.utils import Monitor_NaN

class HMC(BaseTransitionModel):
    """
    Following: https: // arxiv.org / pdf / 1206.1901.pdf
    """
    def __init__(self, n_distributions, epsilon, dim, n_outer=2, L=5, train_params=True,
                 target_p_accept=0.65, lr=1e-3, auto_adjust_step_size=False,
                 tune_period=1000):
        super(HMC, self).__init__()
        self.train_params = train_params
        if train_params:
            self.tune_period = tune_period
            self.counter = 0
            self.log_epsilons = nn.ParameterDict()
            # have to store epsilons like this otherwise we get weird erros
            self.log_epsilons["common"] = nn.Parameter(torch.log(torch.tensor([epsilon])))
            for i in range(n_distributions-2):
                for n in range(n_outer):
                    self.log_epsilons[f"{i}_{n}"] = nn.Parameter(torch.ones(dim))
            self.optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=1e-4)
            self.Monitor_NaN = Monitor_NaN()
            self.register_nan_hooks()
        else:
            self.register_buffer("common_epsilon", torch.tensor([epsilon]))
            self.register_buffer("epsilons", torch.ones([n_distributions, n_outer])*epsilon)
        self.n_outer = n_outer
        self.L = L
        self.n_distributions = n_distributions
        self.auto_adjust_step_size = auto_adjust_step_size
        self.target_p_accept = target_p_accept
        self.first_dist_p_accepts = [torch.tensor([0.0]) for _ in range(n_outer)]
        self.last_dist_p_accepts = [torch.tensor([0.0]) for _ in range(n_outer)]

    def register_nan_hooks(self):
        for parameter in self.parameters():
            # replace with positive number so it decreases step size when we get nans
            parameter.register_hook(
                lambda grad: self.Monitor_NaN.overwrite_NaN_grad(grad, print_=False, replace_with=1.0))

    def interesting_info(self):
        interesting_dict = {}
        if self.n_distributions > 2:
            for i, val in enumerate(self.first_dist_p_accepts):
                interesting_dict[f"dist1_p_accept_{i}"] = val.item()
            if self.n_distributions != 3:
                for i, val in enumerate(self.last_dist_p_accepts):
                    interesting_dict[f"dist{self.n_distributions-3}_p_accept_{i}"] = val.item()
            if self.train_params:
                interesting_dict["epsilon_shared"] = self.log_epsilons["common"].item()
                interesting_dict[f"epsilons_0_0_0"] = self.get_epsilon(0, 0)[0].cpu().item()
                interesting_dict[f"epsilons_0_-1_0"] = self.get_epsilon(0, self.n_outer-1)[0].cpu().item()
            else:
                interesting_dict["epsilon_shared"] = self.common_epsilon.item()
                interesting_dict[f"epsilons_0_0"] = self.log_epsilons[0, 0].cpu().item()
                interesting_dict[f"epsilons_0_-1"] = self.log_epsilons[0, -1].cpu().item()
        return interesting_dict

    def get_epsilon(self, i, n):
        if self.train_params:
            return torch.exp(self.log_epsilons[f"{i}_{n}"]) + torch.exp(self.log_epsilons["common"])
        else:
            return (self.epsilons[i, n] + self.common_epsilon) / 2


    def HMC_func(self, U, current_q, grad_U, i):
        # need this for grad function
        current_q = current_q.clone().detach().requires_grad_(True)
        current_q = torch.clone(current_q)  # so we can do in place operations, kinda weird hack
        # base function for HMC written in terms of potential energy function U
        if self.train_params:
            self.optimizer.zero_grad()
            loss = 0
        for n in range(self.n_outer):
            epsilon = self.get_epsilon(i, n)
            q = current_q
            p = torch.randn_like(q)
            current_p = p
            # make momentum half step
            p = p - epsilon * grad_U(q) / 2

            # Now loop through position and momentum leapfrogs
            for l in range(self.L):
                # Make full step for position
                q = q + epsilon * p
                # Make a full step for the momentum if not at end of trajectory
                if l != self.L-1:
                    p = p - epsilon * grad_U(q)

            # make a half step for momentum at the end
            p = p - epsilon * grad_U(q) / 2
            # Negate momentum at end of trajectory to make proposal symmetric
            p = -p

            U_current = U(current_q)
            U_proposed = U(q)
            current_K = torch.sum(current_p**2, dim=-1) / 2
            proposed_K = torch.sum(p**2, dim=-1) / 2

            # comment out this, as let's rather just maximise the final log prob
            #if self.train_params:
            #    loss = loss + torch.mean(U_proposed)  # - mean log target prob

            # Accept or reject the state at the end of the trajectory, returning either the position at the
            # end of the trajectory or the initial position
            acceptance_probability = torch.exp(U_current - U_proposed + current_K - proposed_K)
            # reject samples with nan acceptance probability
            acceptance_probability = torch.nan_to_num(acceptance_probability, nan=0.0, posinf=0.0, neginf=0.0)
            acceptance_probability = torch.clamp(acceptance_probability, min=0.0, max=1.0)
            accept = acceptance_probability > torch.rand(acceptance_probability.shape).to(q.device)
            current_q[accept] = q[accept]

            if self.auto_adjust_step_size:
                p_accept = torch.mean(acceptance_probability)
                if p_accept > self.target_p_accept: # too much accept
                    self.log_epsilons[i, n] = self.log_epsilons[i, n] * 1.1
                    self.common_epsilon = self.common_epsilon * 1.05
                else:
                    self.log_epsilons[i, n] = self.log_epsilons[i, n] / 1.1
                    self.common_epsilon = self.common_epsilon / 1.05

            if i == 0: # save fist and last distribution info
                # save as interesting info for plotting
                self.first_dist_p_accepts[n] = torch.mean(acceptance_probability).cpu().detach()
            elif i == self.n_distributions - 3:
                self.last_dist_p_accepts[n] = torch.mean(acceptance_probability).cpu().detach()
        if i == 0:
            self.counter += 1
        if self.counter < self.tune_period and self.train_params:
            loss = torch.mean(U_proposed)
            loss.backward() #retain_graph=True)
            grad_norm = torch.nn.utils.clip_grad_norm_(self.parameters(), 1)
            torch.nn.utils.clip_grad_value_(self.parameters(), 1)
            # torch.autograd.grad(loss, self.epsilons["0_1"], retain_graph=True)
            self.optimizer.step()
            current_q = current_q.detach()  # otherwise traces gradient back to epsilon
        return current_q

    def run(self, current_q, log_q_x, i):
        # currently mainly written with grad_log_q_x = None in mind
        # using diagonal, would be better to use vmap (need to wait until this is released doh)
        """
        U is potential energy, q is position, p is momentum
        """
        def U(x: torch.Tensor):
            return - log_q_x(x)

        def grad_U(q: torch.Tensor):
            y = U(q)
            return torch.autograd.grad(y, q, grad_outputs=torch.ones_like(y))[0]

        current_q = self.HMC_func(U, current_q, grad_U, i)
        return current_q

if __name__ == '__main__':
    torch.autograd.set_detect_anomaly(True)
    from TargetDistributions.MoG import MoG
    from FittedModels.Models.DiagonalGaussian import DiagonalGaussian
    import matplotlib.pyplot as plt
    n_samples = 4000
    n_distributions_pretend = 4
    dim = 2
    torch.manual_seed(2)
    target = MoG(dim=dim, n_mixes=5, loc_scaling=5)
    learnt_sampler = DiagonalGaussian(dim=dim, log_std_initial_scaling=2.0)
    hmc = HMC(n_distributions=n_distributions_pretend, n_outer=40, epsilon=1.0, L=6, dim=dim, train_params=True,
              auto_adjust_step_size=False)
    n = 5
    for i in range(n):
        for j in range(n_distributions_pretend-2):
            sampler_samples = learnt_sampler(n_samples)[0]
            x_HMC = hmc.run(sampler_samples, target.log_prob, j)
        if i == 0 or i == n-1:
            print(hmc.interesting_info())


    sampler_samples = sampler_samples.cpu().detach()
    x_HMC = x_HMC.cpu().detach()
    plt.plot(sampler_samples[:, 0], sampler_samples[:, 1], "o", alpha=0.5)
    plt.title("sampler samples")
    plt.show()
    plt.plot(x_HMC[:, 0], x_HMC[:, 1], "o", alpha=0.5)
    plt.title("annealed samples")
    plt.show()
    true_samples = target.sample((n_samples,)).cpu().detach()
    plt.plot(true_samples[:, 0], true_samples[:, 1], "o", alpha=0.5)
    plt.title("true samples")
    plt.show()

