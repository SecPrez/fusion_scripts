"""fusion-cam — query Fusion 360 CAM data via the FusionCAMBridge add-in."""

__version__ = "0.1.0"

from .client import FusionCAMClient

__all__ = ["FusionCAMClient"]
