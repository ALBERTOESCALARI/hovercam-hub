from hub.discovery import parse_probe_match

SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>
  <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Front_Door
   onvif://www.onvif.org/hardware/IPC-T5442 onvif://www.onvif.org/mfr/Dahua</d:Scopes>
  <d:XAddrs>http://192.168.1.64/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def test_parses_onvif_probe_match():
    cam = parse_probe_match(SAMPLE, "192.168.1.64")
    assert cam == {
        "address": "192.168.1.64",
        "xaddr": "http://192.168.1.64/onvif/device_service",
        "name": "Front Door",
        "manufacturer": "Dahua",
        "model": "IPC-T5442",
    }


def test_ignores_garbage():
    assert parse_probe_match(b"not xml", "1.2.3.4") is None
    assert parse_probe_match(b"<a/>", "1.2.3.4") is None
