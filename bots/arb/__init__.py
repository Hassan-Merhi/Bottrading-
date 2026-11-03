"""Arbitrage: measure real net-of-fee opportunities first, paper-trade them second.

Nothing in this package places a real order. It observes live order books on
two or more venues, works out whether buying on one and selling on the other
would have made money after both taker fees, slippage and the book's depth,
and simulates that trade against inventory that already sits on both venues.
Funding-rate carry (long spot, short perpetual) is measured the same way.
"""
