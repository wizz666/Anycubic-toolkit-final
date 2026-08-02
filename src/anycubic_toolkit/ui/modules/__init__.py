"""Sidebar module pages."""

from anycubic_toolkit.ui.modules.about import AboutPage
from anycubic_toolkit.ui.modules.bambu_import import BambuImportPage
from anycubic_toolkit.ui.modules.base import AppContext, ModulePage
from anycubic_toolkit.ui.modules.connect import ConnectPage
from anycubic_toolkit.ui.modules.dashboard import DashboardPage
from anycubic_toolkit.ui.modules.error_lookup import ErrorLookupPage
from anycubic_toolkit.ui.modules.firmware import FirmwareCenterPage
from anycubic_toolkit.ui.modules.health import HealthPage
from anycubic_toolkit.ui.modules.history import HistoryPage
from anycubic_toolkit.ui.modules.log_analyzer import LogAnalyzerPage
from anycubic_toolkit.ui.modules.resources import ResourcesPage
from anycubic_toolkit.ui.modules.printer_info import PrinterInfoPage
from anycubic_toolkit.ui.modules.rinkhals import RinkhalsPage
from anycubic_toolkit.ui.modules.settings import SettingsPage
from anycubic_toolkit.ui.modules.support_report import SupportReportPage

__all__ = [
    "AboutPage",
    "AppContext",
    "BambuImportPage",
    "ConnectPage",
    "DashboardPage",
    "ErrorLookupPage",
    "FirmwareCenterPage",
    "HealthPage",
    "HistoryPage",
    "LogAnalyzerPage",
    "ResourcesPage",
    "ModulePage",
    "PrinterInfoPage",
    "RinkhalsPage",
    "SettingsPage",
    "SupportReportPage",
]
