from TargetDistributions.DoubleWell import DoubleWellEnergy
import torch
from FittedModels.Utils.plotting_utils import plot_sampling_info, plot_divergences
torch.manual_seed(5)
from ImportanceSampling.VanillaImportanceSampler import VanillaImportanceSampling
from FittedModels.Utils.plotting_utils import plot_distributions
from FittedModels.train import LearntDistributionManager
from Utils.plotting_utils import plot_distribution
from Utils.numerical_utils import quadratic_function as expectation_function
from FittedModels.Models.FlowModel import FlowModel
from FittedModels.Utils.plotting_utils import plot_history
import matplotlib.pyplot as plt
from FittedModels.Utils.plotting_utils import plot_samples_vs_contours
if __name__ == '__main__':

    torch.set_default_dtype(torch.float64)
    torch.manual_seed(1)
    # ******************* Parameters *******************
    dim = 2
    epochs = int(1e4)
    n_samples_estimation = int(1e5)
    batch_size = int(1e4)
    lr = 1e-3
    optimizer = "Adamax"
    loss_type = "DReG"  # "kl"  #
    initial_flow_scaling = 8.0
    n_flow_steps = 5
    annealing = True

    target = DoubleWellEnergy(2, a=-0.5, b=-6)
    dist = plot_distribution(target, bounds=[[-3, 3], [-3, 3]], n_points=300)
    plt.show()
    torch.manual_seed(0)  # 0
    learnt_sampler = FlowModel(x_dim=dim, n_flow_steps=n_flow_steps,
                               scaling_factor=initial_flow_scaling)  # , flow_type="RealNVP", use_exp=True
    tester = LearntDistributionManager(target, learnt_sampler, VanillaImportanceSampling, loss_type=loss_type,
                                       lr=lr, optimizer=optimizer, annealing=annealing)

    plot_samples_vs_contours(tester)
    plt.show()
    expectation_before, info_before = tester.estimate_expectation(n_samples_estimation, expectation_function)

    plot_distributions(tester, bounds=[[-3, 3], [-3, 3]], n_points=100)
    plt.show()
    history = tester.train(epochs, batch_size=batch_size, clip_grad_norm=True, max_grad_norm=1,
                           intermediate_plots=True, plotting_func=plot_samples_vs_contours)
    plot_history(history)
    plt.show()
    plot_divergences(history)
    plt.show()
    plot_sampling_info(history)
    plt.show()

    expectation, info = tester.estimate_expectation(n_samples_estimation, expectation_function)
    print(f"estimate before training is {expectation_before} \n"
          f"estimate after training is {expectation} \n"
          f"effective sample size before is {info_before['effective_sample_size'] / n_samples_estimation}\n"
          f"effective sample size after train is {info['effective_sample_size'] / n_samples_estimation}\n"
          f"variance in weights is {torch.var(info['normalised_sampling_weights'])}")

    plot_distributions(tester, bounds=[[-3, 3], [-3, 3]], n_points=100)
    plt.show()




