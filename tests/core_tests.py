# tests/core_tests.py
import unittest
from core.risk_management import check_drawdown  # Import it
from core.regulatory_compliance import validate_order  # Import it

class TestRiskManagement(unittest.TestCase):
    def test_check_drawdown_exceeds_limit(self):
        self.assertTrue(check_drawdown(850000, 1000000, max_drawdown=0.10))
        self.assertFalse(check_drawdown(950000, 1000000, max_drawdown=0.10))

class TestRegulatoryCompliance(unittest.TestCase):
    def test_validate_order_success(self):
        self.assertTrue(validate_order("INFY", 10, "BUY", 1500, "LIMIT"))

    def test_validate_order_invalid_quantity(self):
        self.assertFalse(validate_order("INFY", -10, "BUY", 1500, "LIMIT"))

    def test_validate_options_order(self):
        # Test that it succeeds with an example.
        self.assertTrue(validate_order("INFY2401261700CE", 10, "BUY", 10, "LIMIT")) # Test that the order can go.

    def test_validate_options_order_invalid_expiry(self):
        self.assertFalse(validate_order("INFY2401351700CE", 10, "BUY", 10, "LIMIT")) # Test that a failure to enter data.

    def test_validate_options_order_invalid_option_symbol(self):
        self.assertFalse(validate_order("INVALID_SYMBOL",10,"BUY", 10, "LIMIT")) # Test an invalid one

if __name__ == '__main__':
    unittest.main()