from obsrv.communication.request_solver import RequestSolver
from obsrv.communication.router import Router
from obsrv.tree_components.base_components.tree_base_broker import TreeBaseBroker
from obsrv.tree_components.base_components.tree_base_broker_default_target import TreeBaseBrokerDefaultTarget
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.tree_components.specialized_components import TreeBlockerAccessGrantor
from obsrv.tree_components.specialized_components import TreeCache
from obsrv.tree_components.specialized_components import TreeConditionalFreezer
from obsrv.tree_components.specialized_components import TreeBaseRequestBlocker
from obsrv.tree_components.specialized_components import TreeCustomGuiderHandler
from obsrv.tree_components.specialized_components import TreeEphemeris
from obsrv.tree_components.specialized_components.tree_plan_executor import TreePlanExecutor
from obsrv.ob_config import SingletonConfig


def tree_build() -> Router:
    """This is the Example tree structure, suitable for development. It defines dev telescope.

    This file is loaded by the default configuration file obsrv/config.yaml
    The corresponding components tree is loaded from obsrv/configuration/sample_config.yaml

    In the production environment, configuration should be defined in
        obsrv/configuration/config.yaml or /usr/local/etc/ocabox/config.yaml,
    and the tree should be build be corresponding custom tree_build_xxxx.py
    """

    SingletonConfig.add_config_file_from_config_dir('sample_config.yaml')
    SingletonConfig.get_config(rebuild=True).get()  # this method refreshes the configuration with newly added files,

    # --------------------------------------- global ---------------------------------------
    ephemeris = TreeEphemeris('ephemeris', 'ephemeris')
    broker_components_global = TreeBaseBrokerDefaultTarget('broker-components-global',
                                                         [ephemeris],
                                                         default_provider=None)
    cache_global = TreeCache('cache-global', broker_components_global)
    conditional_freezer_global = TreeConditionalFreezer('conditional-freezer-global', cache_global)
    target_provider_global = TreeProvider('target-provider-global', 'global', conditional_freezer_global)

    # --------------------------------------- sim ---------------------------------------
    alpaca_sim = TreeAlpacaObservatory('alpaca-sim', observatory_name='sim')
    alpaca_blocker_sim = TreeBaseRequestBlocker('alpaca-blocker-sim', alpaca_sim)
    blocker_grantor_sim = TreeBlockerAccessGrantor('access-grantor-sim', 'access_grantor', alpaca_blocker_sim)
    plan_executor_sim = TreePlanExecutor('executor-sim', 'executor')
    broker_components_sim = TreeBaseBrokerDefaultTarget('broker-components-sim',
                                                        [blocker_grantor_sim, plan_executor_sim],
                                                        default_provider=alpaca_blocker_sim)
    cache_sim = TreeCache('cache-sim', broker_components_sim)
    conditional_freezer_sim = TreeConditionalFreezer('conditional-freezer-sim', cache_sim)
    target_provider_sim = TreeProvider('target-provider-sim', 'sim', conditional_freezer_sim)

    # --------------------------------------- dev ---------------------------------------
    alpaca_dev = TreeAlpacaObservatory('alpaca-dev', observatory_name='dev')
    alpaca_blocker_dev = TreeBaseRequestBlocker('alpaca-blocker-dev', alpaca_dev)
    blocker_grantor_dev = TreeBlockerAccessGrantor('access-grantor-dev', 'access_grantor', alpaca_blocker_dev)
    plan_executor_dev = TreePlanExecutor('executor-dev', 'executor')
    custom_guider_handler = TreeCustomGuiderHandler('custom-guider-handler', 'custom_guider', alpaca_dev)
    broker_components_dev = TreeBaseBrokerDefaultTarget('broker-components-dev',
                                                        [blocker_grantor_dev, plan_executor_dev, custom_guider_handler],
                                                        default_provider=alpaca_blocker_dev)
    cache_dev = TreeCache('cache-dev', broker_components_dev)
    conditional_freezer_dev = TreeConditionalFreezer('conditional-freezer-dev', cache_dev)
    target_provider_dev = TreeProvider('target-provider-dev', 'dev', conditional_freezer_dev)

    # --------------------------------------- dummytest ---------------------------------------
    alpaca_dummytest = TreeAlpacaObservatory('alpaca-dummytest', observatory_name='dummytest')
    alpaca_blocker_dummytest = TreeBaseRequestBlocker('alpaca-blocker-dummytest', alpaca_dummytest)
    blocker_grantor_dummytest = TreeBlockerAccessGrantor('access-grantor-dummytest', 'access_grantor', alpaca_blocker_dummytest)
    plan_executor_dummytest = TreePlanExecutor('executor-dummytest', 'executor')
    broker_components_dummytest = TreeBaseBrokerDefaultTarget('broker-components-dummytest',
                                                             [blocker_grantor_dummytest, plan_executor_dummytest],
                                                             default_provider=alpaca_blocker_dummytest)
    cache_dummytest = TreeCache('cache-dummytest', broker_components_dummytest)
    conditional_freezer_dummytest = TreeConditionalFreezer('conditional-freezer-dummytest', cache_dummytest)
    target_provider_dummytest = TreeProvider('target-provider-dummytest', 'dummytest', conditional_freezer_dummytest)


    # -----------------------------gather alpacas components -----------------------------
    broker_front_oca = TreeBaseBroker('broker-front-oca',[
                                       target_provider_sim,
                                       target_provider_dev,
                                       target_provider_dummytest,
                                       target_provider_global
    ])

    # ------------------------------ front receiver blocks -------------------------------
    rs = RequestSolver(data_provider=broker_front_oca)
    vr = Router(request_solver=rs, name="Router-OCA")
    return vr
