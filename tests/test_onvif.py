import xml.etree.ElementTree as ET

from hub.onvif import parse_camera_time, parse_profiles, pick_profile, with_credentials

PROFILES = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><trt:GetProfilesResponse>
 <trt:Profiles token="000"><tt:Name>MainStream</tt:Name><tt:VideoEncoderConfiguration>
  <tt:Encoding>H265</tt:Encoding><tt:Resolution><tt:Width>2560</tt:Width>
  <tt:Height>1440</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>
 <trt:Profiles token="001"><tt:Name>SubStream</tt:Name><tt:VideoEncoderConfiguration>
  <tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>640</tt:Width>
  <tt:Height>360</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>
</trt:GetProfilesResponse></s:Body></s:Envelope>"""


def test_picks_h264_substream_over_h265_main():
    profiles = parse_profiles(ET.fromstring(PROFILES))
    assert [p["token"] for p in profiles] == ["000", "001"]
    assert pick_profile(profiles)["token"] == "001"


def test_prefers_1080p_over_4k_when_both_h264():
    profiles = [
        {"token": "a", "name": "4k", "encoding": "H264", "width": 3840, "height": 2160},
        {"token": "b", "name": "1080", "encoding": "H264", "width": 1920, "height": 1080},
        {"token": "c", "name": "sub", "encoding": "H264", "width": 640, "height": 360},
    ]
    assert pick_profile(profiles)["token"] == "b"


def test_credentials_are_url_encoded_and_host_replaced():
    url = with_credentials("rtsp://0.0.0.0:554/stream1?x=1", "192.168.0.104", "admin", "p@ss:w/rd")
    assert url == "rtsp://admin:p%40ss%3Aw%2Frd@192.168.0.104:554/stream1?x=1"


def test_parses_camera_clock():
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
     xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body><x><tt:UTCDateTime>
     <tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>17</tt:Day></tt:Date>
     <tt:Time><tt:Hour>13</tt:Hour><tt:Minute>5</tt:Minute><tt:Second>9</tt:Second></tt:Time>
     </tt:UTCDateTime></x></s:Body></s:Envelope>"""
    t = parse_camera_time(ET.fromstring(xml))
    assert (t.year, t.hour, t.minute, t.second) == (2026, 13, 5, 9)
