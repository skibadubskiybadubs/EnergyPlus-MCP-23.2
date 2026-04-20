"""
Tests for EnergyPlus MCP Server compatibility with EnergyPlus 23.2.0

These tests verify that the MCP server and its tools work correctly
with EnergyPlus 23.2.0 inside the Docker container.

Run with:
    docker run --rm -v "${PWD}:/workspace" -w /workspace/energyplus-mcp-server \
        energyplus-mcp-dev uv run pytest tests/test_energyplus_v23.py -v
"""

import os
import sys
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_EP_VERSION = "23.2.0"
EXPECTED_INSTALL_DIR = "/app/software/EnergyPlusV23-2-0"

SAMPLE_FILES_DIR = Path(__file__).resolve().parent.parent / "sample_files"

SAMPLE_IDFS = [
    "1ZoneEvapCooler.idf",
    "1ZoneUncontrolled.idf",
    "5ZoneAirCooled.idf",
    "5ZoneAirCooled_with_outputs.idf",
    "AirflowNetwork_MultiZone_SmallOffice_VAV.idf",
    "LgOffVAV.idf",
]

WEATHER_FILE = SAMPLE_FILES_DIR / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"


def _in_docker() -> bool:
    """Check if running inside the Docker container (IDD exists)."""
    return os.path.exists(os.path.join(EXPECTED_INSTALL_DIR, "Energy+.idd"))


needs_docker = pytest.mark.skipif(
    not _in_docker(),
    reason="Requires EnergyPlus Docker container",
)


# ===========================================================================
# 1. Configuration Tests
# ===========================================================================

class TestConfiguration:
    """Tests for config.py version and path defaults."""

    def test_version_string(self):
        from energyplus_mcp_server.config import EnergyPlusConfig
        cfg = EnergyPlusConfig()
        assert cfg.version == EXPECTED_EP_VERSION

    def test_default_installation_path(self):
        from energyplus_mcp_server.config import Config
        # When EPLUS_IDD_PATH is not set, default should point to V23-2-0
        saved = os.environ.pop("EPLUS_IDD_PATH", None)
        try:
            # Force a fresh config
            from energyplus_mcp_server.config import reload_config
            cfg = reload_config()
            assert "EnergyPlusV23-2-0" in cfg.energyplus.installation_path
        finally:
            if saved is not None:
                os.environ["EPLUS_IDD_PATH"] = saved

    def test_config_from_env_var(self):
        """When EPLUS_IDD_PATH is set, config should derive paths from it."""
        from energyplus_mcp_server.config import reload_config
        saved = os.environ.get("EPLUS_IDD_PATH")
        fake_path = "/some/custom/path/Energy+.idd"
        os.environ["EPLUS_IDD_PATH"] = fake_path
        try:
            cfg = reload_config()
            assert cfg.energyplus.idd_path == fake_path
            assert cfg.energyplus.installation_path == "/some/custom/path"
        finally:
            if saved is not None:
                os.environ["EPLUS_IDD_PATH"] = saved
            else:
                os.environ.pop("EPLUS_IDD_PATH", None)
            # Reset the singleton so later tests get the real config
            reload_config()


# ===========================================================================
# 2. EnergyPlus Installation Tests (require Docker)
# ===========================================================================

@needs_docker
class TestEnergyPlusInstallation:
    """Tests that verify EnergyPlus 23.2 is correctly installed."""

    def test_idd_file_exists(self):
        idd = os.path.join(EXPECTED_INSTALL_DIR, "Energy+.idd")
        assert os.path.isfile(idd), f"IDD not found at {idd}"

    def test_executable_exists(self):
        exe = os.path.join(EXPECTED_INSTALL_DIR, "energyplus")
        assert os.path.isfile(exe), f"Executable not found at {exe}"

    def test_weather_data_dir_exists(self):
        d = os.path.join(EXPECTED_INSTALL_DIR, "WeatherData")
        assert os.path.isdir(d)

    def test_example_files_dir_exists(self):
        d = os.path.join(EXPECTED_INSTALL_DIR, "ExampleFiles")
        assert os.path.isdir(d)

    def test_version_output(self):
        import subprocess
        result = subprocess.run(
            ["energyplus", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        assert "23.2.0" in result.stdout


# ===========================================================================
# 3. IDF Loading Tests (require Docker)
# ===========================================================================

@needs_docker
class TestIDFLoading:
    """Test that sample IDF files load correctly with EP 23.2 IDD."""

    @pytest.fixture(autouse=True)
    def setup_eppy(self):
        """Initialize eppy with the EP 23.2 IDD once."""
        from eppy.modeleditor import IDF
        idd = os.path.join(EXPECTED_INSTALL_DIR, "Energy+.idd")
        try:
            IDF.setiddname(idd)
        except eppy.modeleditor.IDDAlreadySetError:
            pass

    @pytest.mark.parametrize("idf_name", SAMPLE_IDFS)
    def test_load_idf(self, idf_name):
        from eppy.modeleditor import IDF
        path = SAMPLE_FILES_DIR / idf_name
        assert path.exists(), f"Sample file missing: {path}"
        idf = IDF(str(path))
        # Should have at least one Zone
        zones = idf.idfobjects.get("Zone", [])
        assert len(zones) > 0, f"{idf_name} has no zones"

    @pytest.mark.parametrize("idf_name", SAMPLE_IDFS)
    def test_version_field(self, idf_name):
        from eppy.modeleditor import IDF
        path = SAMPLE_FILES_DIR / idf_name
        idf = IDF(str(path))
        versions = idf.idfobjects.get("Version", [])
        assert len(versions) > 0, f"{idf_name} missing Version object"
        ver_str = str(versions[0].Version_Identifier)
        assert ver_str.startswith("23.2"), f"{idf_name} version is {ver_str}, expected 23.2"


# ===========================================================================
# 4. Simulation Test (require Docker)
# ===========================================================================

@needs_docker
class TestSimulation:
    """Run a short simulation and verify outputs."""

    @pytest.fixture(autouse=True)
    def setup_eppy(self):
        from eppy.modeleditor import IDF
        idd = os.path.join(EXPECTED_INSTALL_DIR, "Energy+.idd")
        try:
            IDF.setiddname(idd)
        except eppy.modeleditor.IDDAlreadySetError:
            pass

    def test_run_1zone_uncontrolled(self, tmp_path):
        """Run 1ZoneUncontrolled with design day only (fast)."""
        from eppy.modeleditor import IDF
        idf_path = SAMPLE_FILES_DIR / "1ZoneUncontrolled.idf"
        weather_path = str(WEATHER_FILE)
        output_dir = str(tmp_path / "sim_output")
        os.makedirs(output_dir, exist_ok=True)

        idf = IDF(str(idf_path), epw=weather_path)
        idf.run(
            output_directory=output_dir,
            design_day=True,
            readvars=True,
            expandobjects=True,
            output_prefix="1ZoneUncontrolled",
            output_suffix="C",
            verbose="v",
            weather=weather_path,
        )

        # Check that key output files were created
        files = os.listdir(output_dir)
        err_files = [f for f in files if f.endswith(".err")]
        assert len(err_files) > 0, "No .err file produced"

        # Read the .err file and verify no fatal errors
        err_path = os.path.join(output_dir, err_files[0])
        with open(err_path) as f:
            err_content = f.read()
        assert "Fatal" not in err_content, f"Simulation had fatal errors:\n{err_content[-500:]}"


# ===========================================================================
# 5. Tool Integration Tests (require Docker)
# ===========================================================================

@needs_docker
class TestToolIntegration:
    """Test key EnergyPlusManager methods directly."""

    @pytest.fixture(autouse=True)
    def setup_manager(self):
        from energyplus_mcp_server.energyplus_tools import EnergyPlusManager
        self.manager = EnergyPlusManager()

    def test_load_idf_model(self):
        result = self.manager.load_idf(str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf"))
        # load_idf returns a dict directly
        assert result["loaded_successfully"] is True
        assert result["zone_count"] > 0

    def test_get_model_basics(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.get_model_basics(idf_path)
        # Returns JSON string
        import json
        data = json.loads(result)
        assert "building" in data or "Building" in result

    def test_list_zones(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.list_zones(idf_path)
        import json
        data = json.loads(result)
        # May be a list of zones or a dict with zone info
        if isinstance(data, list):
            assert len(data) >= 5  # 5ZoneAirCooled has 5 zones
        else:
            assert data.get("zone_count", len(data.get("zones", []))) >= 5

    def test_get_surfaces(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.get_surfaces(idf_path)
        import json
        data = json.loads(result)
        if isinstance(data, list):
            assert len(data) > 0
        else:
            assert data.get("surface_count", len(data.get("surfaces", []))) > 0

    def test_get_materials(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.get_materials(idf_path)
        import json
        data = json.loads(result)
        if isinstance(data, list):
            assert len(data) > 0
        else:
            assert data.get("material_count", len(data.get("materials", []))) > 0

    def test_validate_idf(self):
        idf_path = str(SAMPLE_FILES_DIR / "1ZoneUncontrolled.idf")
        result = self.manager.validate_idf(idf_path)
        import json
        data = json.loads(result)
        assert data["is_valid"] is not None

    def test_discover_hvac_loops(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.discover_hvac_loops(idf_path)
        import json
        data = json.loads(result)
        # 5ZoneAirCooled should have HVAC data — check that result is not empty
        # The structure varies, so just verify we got valid JSON with content
        assert data is not None
        assert len(str(data)) > 10, "Expected non-trivial HVAC data in 5ZoneAirCooled"

    def test_check_simulation_settings(self):
        idf_path = str(SAMPLE_FILES_DIR / "5ZoneAirCooled.idf")
        result = self.manager.check_simulation_settings(idf_path)
        import json
        data = json.loads(result)
        assert "simulation_control" in data or "SimulationControl" in result


# ===========================================================================
# 6. MCP Server Tests (require Docker)
# ===========================================================================

@needs_docker
class TestMCPServer:
    """Test that the MCP server initializes correctly."""

    def test_server_config_loads(self):
        from energyplus_mcp_server.config import get_config
        cfg = get_config()
        assert cfg.energyplus.version == EXPECTED_EP_VERSION
        assert os.path.exists(cfg.energyplus.idd_path)

    def test_manager_initializes(self):
        from energyplus_mcp_server.energyplus_tools import EnergyPlusManager
        manager = EnergyPlusManager()
        assert manager is not None
        assert manager.config.energyplus.version == EXPECTED_EP_VERSION
