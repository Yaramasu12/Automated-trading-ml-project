"""Tests for Phase 5: RL simulation lab."""
import unittest

from trading_platform.rl.env import (
    TradingSimEnv, ACTION_NOOP, ACTION_PROPOSE_ENTRY,
    ACTION_PROPOSE_EXIT, N_ACTIONS,
)
from trading_platform.rl.policies import MockPolicy, RandomPolicy, PolicyRegistry, PolicyRecord
from trading_platform.rl.evaluator import RLEvaluator


class TestTradingSimEnv(unittest.TestCase):
    def test_reset_returns_obs(self):
        env = TradingSimEnv()
        obs = env.reset()
        self.assertIsNotNone(obs)
        self.assertIsInstance(obs.to_list(), list)

    def test_step_returns_result(self):
        env = TradingSimEnv()
        env.reset()
        result = env.step(ACTION_NOOP)
        self.assertFalse(result.done or result.done is None)
        self.assertIsInstance(result.reward, float)

    def test_entry_increases_positions(self):
        env = TradingSimEnv()
        env.reset()
        before = env._positions
        env.step(ACTION_PROPOSE_ENTRY)
        self.assertEqual(env._positions, before + 1)

    def test_exit_decreases_positions(self):
        env = TradingSimEnv()
        env.reset()
        env.step(ACTION_PROPOSE_ENTRY)
        env.step(ACTION_PROPOSE_ENTRY)
        before = env._positions
        env.step(ACTION_PROPOSE_EXIT)
        self.assertEqual(env._positions, before - 1)

    def test_done_after_max_steps(self):
        env = TradingSimEnv(max_steps=5)
        env.reset()
        done = False
        for _ in range(10):
            result = env.step(ACTION_NOOP)
            if result.done:
                done = True
                break
        self.assertTrue(done)

    def test_reward_penalizes_drawdown(self):
        returns = [-0.05] * 252  # Heavily negative
        env = TradingSimEnv(returns=returns, drawdown_penalty=5.0)
        env.reset()
        env.step(ACTION_PROPOSE_ENTRY)
        result = env.step(ACTION_PROPOSE_ENTRY)
        # With heavy losses + high penalty, reward should be negative
        self.assertLess(result.reward, 0.0)

    def test_observation_space_dim(self):
        env = TradingSimEnv()
        env.reset()
        dim = env.observation_space_dim()
        self.assertGreater(dim, 0)

    def test_action_space_size(self):
        env = TradingSimEnv()
        self.assertEqual(env.action_space_size(), N_ACTIONS)


class TestMockPolicy(unittest.TestCase):
    def test_cannot_submit_live_orders(self):
        policy = MockPolicy()
        self.assertFalse(policy.can_submit_live_orders)

    def test_fixed_action(self):
        policy = MockPolicy(fixed_action=ACTION_NOOP)
        env = TradingSimEnv()
        obs = env.reset()
        self.assertEqual(policy.act(obs), ACTION_NOOP)

    def test_no_live_order_flag(self):
        for action in range(N_ACTIONS):
            policy = MockPolicy(fixed_action=action)
            self.assertFalse(policy.can_submit_live_orders)


class TestPolicyRegistry(unittest.TestCase):
    def test_register_and_get(self):
        registry = PolicyRegistry()
        policy = MockPolicy()
        record = PolicyRecord(policy_id="p1", role="entry")
        registry.register(record, policy)
        self.assertEqual(registry.get("p1"), policy)

    def test_promote(self):
        registry = PolicyRegistry()
        record = PolicyRecord(policy_id="p2", role="exit")
        registry.register(record, MockPolicy())
        ok = registry.promote("p2", "shadow")
        self.assertTrue(ok)
        self.assertEqual(registry.get_record("p2").status, "shadow")

    def test_invalid_status_rejected(self):
        registry = PolicyRegistry()
        record = PolicyRecord(policy_id="p3", role="sizing")
        registry.register(record, MockPolicy())
        ok = registry.promote("p3", "invalid_status")
        self.assertFalse(ok)

    def test_list_all(self):
        registry = PolicyRegistry()
        for i in range(3):
            registry.register(PolicyRecord(f"p{i}", "entry"), MockPolicy())
        all_policies = registry.list_all()
        self.assertEqual(len(all_policies), 3)


class TestRLEvaluator(unittest.TestCase):
    def test_returns_eval_result(self):
        returns = [0.001, -0.002, 0.003, -0.001, 0.002] * 20
        policy = MockPolicy()
        evaluator = RLEvaluator()
        result = evaluator.evaluate(policy, "test-policy", returns, n_episodes=2)
        self.assertIsNotNone(result)
        self.assertEqual(result.policy_id, "test-policy")
        self.assertTrue(result.advisory_only)

    def test_policy_advisory_flag(self):
        result = RLEvaluator().evaluate(
            MockPolicy(), "p", [0.01, -0.01] * 10, n_episodes=1
        )
        self.assertTrue(result.advisory_only)


if __name__ == "__main__":
    unittest.main()
