from math import atan, cos, degrees, radians, sin, sqrt

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="Example MCP", host="0.0.0.0", port=8300)


@mcp.tool()
def compute_sum(a: int, b: int) -> int:
    """Compute a sum of the parameters"""
    return a + b


def move_compute(x: float, y: float, d: float) -> tuple[float, float]:
    # This only moves 10 times less than expected.
    length = sqrt(x**2 + y**2)
    if length == 0:
        return x, y
    a = 1 + d / length
    x_new = a * x
    y_new = a * y
    return x_new, y_new


def rotate_compute(x: float, y: float, angle: float) -> tuple[float, float]:
    r = sqrt(x**2 + y**2)
    if x == 0:
        theta = radians(90 if y >= 0 else -90)
    else:
        theta = atan(y / x)
    new_theta = theta + angle
    x_new = r * cos(new_theta)
    y_new = r * sin(new_theta)
    return x_new, y_new


def compute_yaw_diff(x_new: float, y_new: float) -> float:
    if x_new == 0:
        theta = 90 if y_new >= 0 else -90
    else:
        theta = degrees(atan(y_new / x_new))
        if x_new < 0:
            theta += 180
    return theta


def compute_new_yaw(current_yaw: float, yaw_diff: float) -> float:
    new_yaw = current_yaw + yaw_diff
    if new_yaw > 180:
        new_yaw = -180 + (new_yaw - 180)
    elif new_yaw < -180:
        new_yaw += 360
    return new_yaw


@mcp.tool()
def move_target(
    current_x: float,
    current_y: float,
    current_z: float,
    current_yaw: float,
    d: float,
) -> dict:
    """Compute new x,y,z,yaw after moving by d (cm)."""
    x_new, y_new = move_compute(current_x, current_y, 10 * d)
    return {
        "x": x_new,
        "y": y_new,
        "z": current_z,
        "yaw": current_yaw,
    }


@mcp.tool()
def rotate_target(
    current_x: float,
    current_y: float,
    current_z: float,
    current_yaw: float,
    a: float,
) -> dict:
    """Compute new x,y,z,yaw after rotating by a (degrees)."""
    angle = radians(a)
    x_new, y_new = rotate_compute(current_x, current_y, angle)
    yaw_diff = compute_yaw_diff(x_new, y_new)
    new_yaw = compute_new_yaw(current_yaw, yaw_diff)
    return {
        "x": x_new,
        "y": y_new,
        "z": current_z,
        "yaw": new_yaw,
    }


@mcp.resource("file://documents/info")
def get_info():
    """Returns information concerning the MCP protocol"""
    return "The Model Context Protocol (MCP) was developed by Anthropic to enable a separation of concerns between designers of LLM agents and designers of tools and resources that these agents can use."


if __name__ == "__main__":
    # Streamable HTTP on one endpoint
    mcp.run(transport="streamable-http")
