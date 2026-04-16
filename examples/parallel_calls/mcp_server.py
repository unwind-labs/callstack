"""
MCP server for parallel_calls example.

Provides simple deterministic tools that sub-agents call.
Each tool returns a fixed value so results are predictable and testable.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("parallel-tasks-backend")


@mcp.tool()
def get_weather(city: str) -> dict:
    """Get the current weather for a city."""
    data = {
        "tokyo": {"city": "Tokyo", "temp_c": 22, "condition": "sunny"},
        "london": {"city": "London", "temp_c": 14, "condition": "cloudy"},
        "new_york": {"city": "New York", "temp_c": 18, "condition": "rainy"},
    }
    return data.get(city.lower(), {"city": city, "temp_c": 0, "condition": "unknown"})


@mcp.tool()
def get_exchange_rate(currency: str) -> dict:
    """Get the exchange rate for a currency relative to USD."""
    rates = {
        "JPY": {"currency": "JPY", "rate": 149.50, "direction": "USD→JPY"},
        "GBP": {"currency": "GBP", "rate": 0.79, "direction": "USD→GBP"},
        "EUR": {"currency": "EUR", "rate": 0.92, "direction": "USD→EUR"},
    }
    return rates.get(currency.upper(), {"currency": currency, "rate": 1.0, "direction": "unknown"})


@mcp.tool()
def get_stock_price(symbol: str) -> dict:
    """Get the current stock price for a ticker symbol."""
    prices = {
        "AAPL": {"symbol": "AAPL", "price": 195.50, "change": +2.30},
        "GOOGL": {"symbol": "GOOGL", "price": 178.25, "change": -1.10},
        "MSFT": {"symbol": "MSFT", "price": 420.80, "change": +5.60},
    }
    return prices.get(symbol.upper(), {"symbol": symbol, "price": 0.0, "change": 0.0})


@mcp.tool()
def get_news_headline(topic: str) -> dict:
    """Get the latest news headline for a topic."""
    headlines = {
        "tech": {"topic": "tech", "headline": "AI Models Surpass Human Performance on Complex Reasoning Tasks"},
        "finance": {"topic": "finance", "headline": "Federal Reserve Holds Interest Rates Steady at 5.25%"},
        "sports": {"topic": "sports", "headline": "World Cup Qualifiers: Unexpected Upsets Across All Groups"},
    }
    return headlines.get(topic.lower(), {"topic": topic, "headline": "No headline available"})


@mcp.tool()
def get_flight_status(flight: str) -> dict:
    """Get the status of a flight."""
    flights = {
        "AA100": {"flight": "AA100", "route": "JFK→LHR", "status": "on_time", "departure": "14:30"},
        "UA200": {"flight": "UA200", "route": "SFO→NRT", "status": "delayed", "departure": "16:45"},
    }
    return flights.get(flight.upper(), {"flight": flight, "status": "not_found"})


if __name__ == "__main__":
    mcp.run()
