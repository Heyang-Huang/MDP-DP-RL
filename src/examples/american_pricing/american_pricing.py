from typing import Callable, Sequence, Tuple, Set
import numpy as np
from algorithms.td_algo_enum import TDAlgorithm
from algorithms.rl_func_approx.tdlambda import TDLambda
from src.examples.american_pricing.num_utils import get_future_price_mean_var
from processes.mdp_rep_for_rl_fa import MDPRepForRLFA
from algorithms.func_approx_spec import FuncApproxSpec
from func_approx.dnn_spec import DNNSpec
from random import choice

StateType = Tuple[float, np.ndarray]
ActionType = bool


class AmericanPricing:
    """
    In the risk-neutral measure, the underlying price x_t
    follows the Ito process: dx_t = r_t x_t dt + dispersion(t, x_t) dz_t
    spot_price is x_0
    payoff is a function from (t, (x_0, ..., x_t) to payoff (
    eg: \sum_{i=0}^t x_i / (t+1) - K)
    expiry is the time to expiry of american option (in years)
    dispersion(t, x_t) is a function from (t, x_t) to dispersion
    We define ir_t = \int_0^t r_u du, so discount D_t = e^{- ir_t}
    where r_t is the infinitesimal risk-free rate at time t
    """

    def __init__(
        self,
        spot_price: float,
        payoff: Callable[[float, np.ndarray], float],
        expiry: float,
        dispersion: Callable[[float, float], float],
        ir: Callable[[float], float]
    ) -> None:
        self.spot_price: float = spot_price
        self.payoff: Callable[[float, np.ndarray], float] = payoff
        self.expiry: float = expiry
        self.dispersion: Callable[[float, float], float] = dispersion
        self.ir: Callable[[float], float] = ir

    def get_ls_price(
        self,
        num_dt: int,
        num_paths: int,
        feature_funcs: Sequence[Callable[[float, np.ndarray], float]]
    ) -> float:
        dt = self.expiry / num_dt
        paths = np.empty([num_paths, num_dt + 1])
        paths[:, 0] = self.spot_price
        for i in range(num_paths):
            price = self.spot_price
            for t in range(num_dt):
                m, v = get_future_price_mean_var(
                    price,
                    t,
                    dt,
                    self.ir,
                    self.dispersion
                )
                price = np.random.normal(m, np.sqrt(v))
                paths[i, t + 1] = price
        cashflow = np.array([max(self.payoff(self.expiry, paths[i, :]), 0.)
                             for i in range(num_paths)])
        for t in range(num_dt - 1, 0, -1):
            """
            For each time slice t
            Step 1: collect X as features of (t, [S_0,.., S_t]) for those paths
            for which payoff(t, [S_0, ...., S_t]) > 0, and corresponding Y as
            the time-t discounted future actual cash flow on those paths.
            Step 2: Do the (X,Y) regression. Denote Y^ as regression-prediction.
            Compare Y^ versus payoff(t, [S_0, ..., S_t]). If payoff is higher,
            set cashflow at time t on that path to be the payoff, else set 
            cashflow at time t on that path to be the time-t discounted future
            actual cash flow on that path.
            """
            disc = np.exp(self.ir(t) - self.ir(t + dt))
            cashflow = cashflow * disc
            payoff = np.array([self.payoff(t, paths[i, :(t + 1)]) for
                               i in range(num_paths)])
            indices = [i for i in range(num_paths) if payoff[i] > 0]
            if len(indices) > 0:
                x_vals = np.array([[f(t, paths[i, :(t + 1)]) for f in
                                    feature_funcs] for i in indices])
                y_vals = np.array([cashflow[i] for i in indices])
                estimate = x_vals.dot(
                    np.linalg.lstsq(x_vals, y_vals, rcond=None)[0]
                )
                # plt.scatter([paths[i, t] for i in indices], y_vals, c='r')
                # plt.scatter([paths[i, t] for i in indices], estimate, c='b')
                # plt.show()

                for i, ind in enumerate(indices):
                    if payoff[ind] > estimate[i]:
                        cashflow[ind] = payoff[ind]

        return max(
            self.payoff(0, np.array([self.spot_price])),
            np.average(cashflow * np.exp(-self.ir(dt)))
        )

    def state_reward_gen(
        self,
        state: StateType,
        action: ActionType,
        delta_t: float
    ) -> Tuple[StateType, float]:
        t, price_arr = state
        reward = np.exp(-self.ir(t)) * self.payoff(t, price_arr) if action else 0.
        m, v = get_future_price_mean_var(
            price_arr[-1],
            t,
            delta_t,
            self.ir,
            self.dispersion
        )
        next_price = np.random.normal(m, np.sqrt(v))
        price1 = np.append(price_arr, next_price)
        next_t = (self.expiry if action else t) + delta_t
        return (next_t, price1), reward

    def get_tdl_obj(
        self,
        num_dt: int,
        algorithm: TDAlgorithm,
        softmax: bool,
        epsilon: float,
        epsilon_half_life: float,
        lambd: float,
        num_episodes: int,
        neurons: Sequence[int],
        learning_rate: float,
        offline: bool
    ) -> TDLambda:
        dt = self.expiry / num_dt

        def sa_func(_: StateType) -> Set[ActionType]:
            return {True, False}

        def terminal_state(
            s: StateType
        ) -> bool:
            return s[0] > self.expiry

        # noinspection PyShadowingNames
        def sr_func(
            s: StateType,
            a: ActionType,
            dt=dt
        ) -> Tuple[StateType, float]:
            return self.state_reward_gen(s, a, dt)

        def init_s() -> StateType:
            return 0., np.array([self.spot_price])

        def init_sa() -> Tuple[StateType, ActionType]:
            return init_s(), choice([True, False])

        # noinspection PyShadowingNames
        mdp_rep_obj = MDPRepForRLFA(
            state_action_func=sa_func,
            gamma=1.,
            terminal_state_func=terminal_state,
            state_reward_gen_func=sr_func,
            init_state_gen=init_s,
            init_state_action_gen=init_sa
        )

        return TDLambda(
            mdp_rep_for_rl=mdp_rep_obj,
            algorithm=algorithm,
            softmax=softmax,
            epsilon=epsilon,
            epsilon_half_life=epsilon_half_life,
            lambd=lambd,
            num_episodes=num_episodes,
            max_steps=num_dt + 1,
            fa_spec=FuncApproxSpec(
                state_feature_funcs=[
                    lambda s: s[0],
                    lambda s: s[1][-1]
                ],
                action_feature_funcs=[
                    lambda a: 1. if a else 0.,
                    lambda a: 0. if a else 1.
                ],
                dnn_spec=DNNSpec(
                    neurons=neurons,
                    hidden_activation=DNNSpec.log_squish,
                    hidden_activation_deriv=DNNSpec.log_squish_deriv,
                    output_activation=DNNSpec.pos_log_squish,
                    output_activation_deriv=DNNSpec.pos_log_squish_deriv
                ),
                learning_rate=learning_rate
            ),
            offline=offline
        )


if __name__ == '__main__':
    spot_price_val = 80.0
    strike_val = 85.0
    payoff_func = lambda _, x: x[-1] - strike_val
    expiry_val = 3.0
    r_val = 0.03
    sigma_val = 0.25

    from examples.american_pricing.bs_pricing import EuropeanBSPricing
    ebsp = EuropeanBSPricing(
        is_call=True,
        spot_price=spot_price_val,
        strike=strike_val,
        expiry=expiry_val,
        r=r_val,
        sigma=sigma_val
    )
    print(ebsp.option_price)
    # noinspection PyShadowingNames
    dispersion_func = lambda t, x, sigma_val=sigma_val: sigma_val * x
    # noinspection PyShadowingNames
    ir_func = lambda t, r_val=r_val: r_val * t

    gp = AmericanPricing(
        spot_price=spot_price_val,
        payoff=payoff_func,
        expiry=expiry_val,
        dispersion=dispersion_func,
        ir=ir_func
    )
    dt_val = 0.1
    num_dt_val = int(expiry_val / dt_val)
    num_paths_val = 10000
    from numpy.polynomial.laguerre import lagval
    num_laguerre = 10
    ident = np.eye(num_laguerre)

    # noinspection PyShadowingNames
    def feature_func(
        _: float,
        x: np.ndarray,
        i: int,
        ident=ident
    ) -> float:
        # noinspection PyTypeChecker
        return lagval(x[-1], ident[i])

    ls_price = gp.get_ls_price(
        num_dt=num_dt_val,
        num_paths=num_paths_val,
        feature_funcs=[(lambda t, x, i=i: feature_func(t, x, i)) for i in
                       range(num_laguerre)]
    )
    print(ls_price)

    algorithm_val = TDAlgorithm.ExpectedSARSA
    softmax_val = True
    epsilon_val = 0.05
    epsilon_half_life_val = 1000
    lambd_val = 0.8
    num_episodes_val = 10000
    neurons_val = [3, 4]
    learning_rate_val = 0.1
    offline_val = False

    rl_fa_obj = gp.get_tdl_obj(
        num_dt=num_dt_val,
        algorithm=algorithm_val,
        softmax=softmax_val,
        epsilon=epsilon_val,
        epsilon_half_life=epsilon_half_life_val,
        lambd=lambd_val,
        num_episodes=num_episodes_val,
        neurons=neurons_val,
        learning_rate=learning_rate_val,
        offline=offline_val
    )
    vf = rl_fa_obj.get_optimal_value_func()
    rl_price = vf((0., spot_price_val))
    print(rl_price)
