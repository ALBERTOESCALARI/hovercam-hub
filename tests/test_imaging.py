import xml.etree.ElementTree as ET

import pytest

from hub.imaging import login_from_rtsp, parse_options, parse_settings, settings_body
from hub.onvif import OnvifError

SETTINGS = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><timg:GetImagingSettingsResponse><timg:ImagingSettings>
 <tt:BacklightCompensation><tt:Mode>OFF</tt:Mode></tt:BacklightCompensation>
 <tt:Brightness>50</tt:Brightness><tt:ColorSaturation>48.5</tt:ColorSaturation>
 <tt:Contrast>50</tt:Contrast><tt:IrCutFilter>AUTO</tt:IrCutFilter><tt:Sharpness>30</tt:Sharpness>
</timg:ImagingSettings></timg:GetImagingSettingsResponse></s:Body></s:Envelope>"""

OPTIONS = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><timg:GetOptionsResponse><timg:ImagingOptions>
 <tt:Brightness><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Brightness>
 <tt:ColorSaturation><tt:Min>0</tt:Min><tt:Max>255</tt:Max></tt:ColorSaturation>
 <tt:IrCutFilterModes>ON</tt:IrCutFilterModes><tt:IrCutFilterModes>OFF</tt:IrCutFilterModes>
 <tt:IrCutFilterModes>AUTO</tt:IrCutFilterModes>
</timg:ImagingOptions></timg:GetOptionsResponse></s:Body></s:Envelope>"""


def test_parse_settings():
    s = parse_settings(ET.fromstring(SETTINGS))
    assert s == {"brightness": 50.0, "saturation": 48.5, "contrast": 50.0,
                 "ir_cut": "AUTO", "sharpness": 30.0}


def test_parse_options():
    o = parse_options(ET.fromstring(OPTIONS))
    assert o["ranges"] == {"brightness": [0.0, 100.0], "saturation": [0.0, 255.0]}
    assert o["ir_cut_modes"] == ["ON", "OFF", "AUTO"]


def test_settings_body_keeps_onvif_order_and_values():
    body = settings_body("src0", {"sharpness": 40, "ir_cut": "off", "brightness": 61.0})
    assert body.index("Brightness") < body.index("IrCutFilter") < body.index("Sharpness")
    assert ">61<" in body and ">OFF<" in body and ">40<" in body
    assert "<timg:VideoSourceToken>src0</timg:VideoSourceToken>" in body
    assert "ColorSaturation" not in body


def test_settings_body_rejects_empty_or_bad():
    with pytest.raises(OnvifError):
        settings_body("src0", {"ir_cut": "sideways"})


def test_login_from_rtsp_decodes_special_characters():
    url = "rtsp://admin:Pass%23word%25@192.168.0.108:554/user=admin_password=x_channel=1"
    assert login_from_rtsp(url) == ("192.168.0.108", "admin", "Pass#word%")
