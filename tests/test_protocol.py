import struct
import unittest

from scenebuilder import artnet, fixtures, rdm


class ArtNetTests(unittest.TestCase):
    def test_port_address(self):
        self.assertEqual(artnet.port_address(0, 0, 0), 0)
        self.assertEqual(artnet.port_address(1, 2, 3), 0x123)
        self.assertEqual(artnet.split_port_address(0x7FFF), (127, 15, 15))
        with self.assertRaises(ValueError):
            artnet.port_address(0, 16, 0)

    def test_poll(self):
        p = artnet.build_poll()
        self.assertTrue(p.startswith(b"Art-Net\x00"))
        self.assertEqual(artnet.opcode(p), 0x2000)
        self.assertEqual(p[10:12], b"\x00\x0e")  # ProtVer 14, big-endian

    def test_poll_reply_roundtrip(self):
        b = artnet.build_poll_reply("2.1.2.3", 1, 2, [3], "E-box Remote", "Anolis E-box", mac=b"\x01\x02\x03\x04\x05\x06")
        self.assertEqual(len(b), 239)
        pr = artnet.parse_poll_reply(b)
        self.assertEqual(pr.ip, "2.1.2.3")
        self.assertEqual(pr.short_name, "E-box Remote")
        self.assertEqual(pr.output_port_addresses, [0x123])
        self.assertTrue(pr.rdm_capable)
        self.assertEqual(pr.mac, "01:02:03:04:05:06")

    def test_dmx(self):
        pkt = artnet.build_dmx(0x123, bytes(range(10)), sequence=7)
        self.assertEqual(artnet.opcode(pkt), 0x5000)
        self.assertEqual(pkt[12], 7)
        self.assertEqual(pkt[14], 0x23)  # SubUni low byte
        self.assertEqual(pkt[15], 0x01)  # Net
        self.assertEqual(struct.unpack(">H", pkt[16:18])[0], 10)
        d = artnet.parse_dmx(pkt)
        self.assertEqual(d["port_address"], 0x123)
        self.assertEqual(d["data"], bytes(range(10)))
        odd = artnet.build_dmx(0, b"\x01\x02\x03")
        self.assertEqual(struct.unpack(">H", odd[16:18])[0], 4)  # padded to even
        self.assertEqual(len(artnet.build_dmx(0, bytes(512))), 18 + 512)

    def test_tod(self):
        req = artnet.build_tod_request(0x123)
        self.assertEqual(req[21], 0x01)   # Net
        self.assertEqual(req[22], 0x00)   # TodFull
        self.assertEqual(req[23], 1)      # AdCount
        self.assertEqual(req[24], 0x23)
        self.assertEqual(artnet.parse_tod_request(req), [0x123])
        uids = [bytes([0x52, 0x53, 0, 0, 0, i]) for i in range(3)]
        td = artnet.parse_tod_data(artnet.build_tod_data(0x123, uids))
        self.assertEqual(td.port_address, 0x123)
        self.assertEqual(td.total, 3)
        self.assertEqual(td.uids, uids)
        ctl = artnet.parse_tod_control(artnet.build_tod_control(0x045))
        self.assertEqual(ctl, {"port_address": 0x045, "command": 1})


class RdmTests(unittest.TestCase):
    def test_build_parse_checksum(self):
        dest = bytes([0x52, 0x53, 0, 0, 0, 9])
        m = rdm.build_request(dest, 5, rdm.GET, rdm.PID_DEVICE_INFO)
        self.assertEqual(m[0], 0x01)
        self.assertEqual(m[1], 24)                     # message length incl. start code, no PD
        self.assertEqual(len(m), 23 + 2)               # without start code, plus checksum
        ck = struct.unpack(">H", m[-2:])[0]
        self.assertEqual(ck, (0xCC + sum(m[:-2])) & 0xFFFF)
        p = rdm.parse(m)
        self.assertTrue(p.checksum_ok)
        self.assertEqual(p.dest, dest)
        self.assertEqual(p.pid, 0x0060)
        self.assertEqual(p.tn, 5)
        self.assertEqual(p.cc, rdm.GET)
        # with start code
        self.assertTrue(rdm.parse(b"\xcc" + m).checksum_ok)
        # corrupt
        bad = bytearray(m)
        bad[-1] ^= 1
        self.assertFalse(rdm.parse(bytes(bad)).checksum_ok)

    def test_set_with_pd(self):
        m = rdm.build_request(b"\x00" * 6, 1, rdm.SET, rdm.PID_DMX_START_ADDRESS, struct.pack(">H", 100))
        self.assertEqual(m[1], 26)
        p = rdm.parse(m)
        self.assertEqual(p.pd, b"\x00\x64")

    def test_device_info(self):
        pd = rdm.encode_device_info(0x0C01, 0x0101, 1, 3, 11, 13, 42)
        self.assertEqual(len(pd), 19)
        d = rdm.decode_device_info(pd)
        self.assertEqual((d["footprint"], d["personality"], d["personality_count"], d["address"]), (3, 11, 13, 42))

    def test_uid_str(self):
        u = bytes([0x52, 0x53, 0x00, 0xA1, 0x00, 0x01])
        self.assertEqual(rdm.uid_str(u), "5253:00A10001")
        self.assertEqual(rdm.uid_from_str("5253:00A10001"), u)

    def test_nack_reason(self):
        msg = rdm.parse(rdm.build(b"\x00" * 6, b"\x01" * 6, 1, rdm.NACK, rdm.SET_RESPONSE, 0xF0, b"\x00\x06"))
        self.assertEqual(msg.nack_reason(), "data out of range")


class FixtureRenderTests(unittest.TestCase):
    def test_all_mode_footprints_match_chart(self):
        expected = {1: 4, 2: 3, 3: 12, 4: 3, 5: 6, 6: 8, 7: 15, 11: 3, 12: 4, 13: 2}
        for m in fixtures.mode_catalog():
            self.assertEqual(m["footprint"], expected[m["mode"]], m)
            self.assertEqual(len(fixtures.render(m["variant"], m["mode"], {})), m["footprint"])

    def test_ctc_anchors(self):
        for v, k in fixtures.CTC_ANCHORS:
            self.assertEqual(fixtures.kelvin_to_ctc(k), v)
        self.assertEqual(fixtures.kelvin_to_ctc(3000), 81)
        self.assertEqual(fixtures.kelvin_to_ctc(100), 21)

    def test_tw_mode11(self):
        self.assertEqual(fixtures.render("TW", 11, {"dim": 1.0, "cct": 2700}), bytes([0, 255, 255]))
        self.assertEqual(fixtures.render("TW", 11, {"dim": 0.5, "cct": 6500})[0], 255)
        d = fixtures.render("TW", 11, {"dim": 0.5, "cct": 4600})
        self.assertEqual(d[0], 128)
        self.assertEqual((d[1] << 8) | d[2], 32768)

    def test_tw_mode12_mix(self):
        warm = fixtures.render("TW", 12, {"dim": 1, "cct": 2700})
        self.assertEqual(warm[:2], bytes([255, 0]))
        mid = fixtures.render("TW", 12, {"dim": 1, "cct": 4600})
        self.assertEqual(mid[:2], bytes([255, 255]))

    def test_rgbw_mode7_safe_defaults(self):
        d = fixtures.render("RGBW", 7, {"dim": 1, "kind": "color", "hue": 0, "sat": 1})
        roles = fixtures.mode_info("RGBW", 7)["roles"]
        v = dict(zip(roles, d))
        self.assertEqual(v["special"], 0)
        self.assertEqual(v["r"], 255)
        self.assertEqual(v["g"], 0)
        self.assertEqual(v["shutter"], 255)   # open (0-31 would be closed)
        self.assertEqual(v["vcw"], 0)
        self.assertEqual(v["ctc"], 0)
        self.assertEqual(v["gc"], 128)
        self.assertEqual(v["dim"], 255)
        s = fixtures.render("RGBW", 7, {"dim": 1, "kind": "white", "cct": 3200}, special=1)
        v = dict(zip(roles, s))
        self.assertEqual(v["special"], 1)
        self.assertEqual(v["ctc"], 91)

    def test_mode1_scales_by_dim(self):
        d = fixtures.render("RGBW", 1, {"dim": 0.5, "kind": "color", "hue": 120, "sat": 1, "white": 1})
        self.assertEqual(d, bytes([0, 128, 0, 128]))
        w = fixtures.render("RGBW", 1, {"dim": 1, "kind": "white"})
        self.assertEqual(w, bytes([0, 0, 0, 255]))

    def test_pw(self):
        self.assertEqual(fixtures.render("PW", 13, {"dim": 1}), b"\xff\xff")


if __name__ == "__main__":
    unittest.main()


class TunedWhiteTests(unittest.TestCase):
    CAL = {"5600": [0.6, 0.8, 0.6, 1.0], "6500": [0.8, 1.0, 0.9, 1.0]}

    def test_below_coolest_uses_fixture_ctc(self):
        d = fixtures.render("RGBW", 7, {"dim": 1, "kind": "white", "cct": 3200}, cal=self.CAL)
        v = dict(zip(fixtures.mode_info("RGBW", 7)["roles"], d))
        self.assertEqual(v["ctc"], 91)

    def test_tuned_point_used_exactly(self):
        d = fixtures.render("RGBW", 7, {"dim": 1, "kind": "white", "cct": 6500}, cal=self.CAL)
        v = dict(zip(fixtures.mode_info("RGBW", 7)["roles"], d))
        self.assertEqual((v["ctc"], v["r"], v["g"], v["w"]), (0, 204, 255, 255))

    def test_blends_between_tuned_points(self):
        mix = fixtures.tuned_mix(self.CAL, 6050)
        self.assertAlmostEqual(mix[0], 0.7, places=3)

    def test_mode1_uses_tuned_mix_too(self):
        d = fixtures.render("RGBW", 1, {"dim": 1, "kind": "white", "cct": 6500}, cal=self.CAL)
        self.assertEqual(list(d), [204, 255, 230, 255])


class MixValuesTests(unittest.TestCase):
    def test_hue_235_is_nearly_pure_blue(self):
        m = fixtures.mix_values("RGBW", 7, {"dim": 0.8, "kind": "color", "hue": 235, "sat": 1})
        self.assertEqual((m["r"], m["g"], m["b"], m["w"], m["dim"]), (0, 21, 255, 0, 80))

    def test_fixture_white_reports_kelvin(self):
        m = fixtures.mix_values("RGBW", 7, {"dim": 1, "kind": "white", "cct": 3200})
        self.assertEqual(m["ctc_k"], 3200)
        self.assertEqual(m["r"], 255)

    def test_mode1_white_is_white_led_only(self):
        m = fixtures.mix_values("RGBW", 1, {"dim": 1, "kind": "white", "cct": 3200})
        self.assertEqual((m["r"], m["g"], m["b"], m["w"]), (0, 0, 0, 255))


class TunableWhiteMode7Tests(unittest.TestCase):
    """TW Calumma in Mode 7 (field test): CTC ignored, R+B = cool LED, G+W = warm LED."""

    def _rgbw(self, k):
        from scenebuilder import fixtures
        d = fixtures.render("TW", 7, {"dim": 0.8, "kind": "white", "cct": k}, special=0)
        return d[1], d[3], d[5], d[7], d[10], d[13]  # r g b w ctc dim

    def test_warm_end_is_warm_leds_only(self):
        r, g, b, w, ctc, dim = self._rgbw(3000)
        self.assertEqual((r, b), (0, 0))
        self.assertEqual((g, w), (255, 255))
        self.assertEqual(ctc, 0)
        self.assertEqual(dim, 204)

    def test_cool_end_is_cool_leds_only(self):
        r, g, b, w, _, _ = self._rgbw(6500)
        self.assertEqual((r, b, g, w), (255, 255, 0, 0))

    def test_even_brightness_across_range(self):
        for k in (3000, 3500, 4100, 5000, 6500):
            r, g, b, w, _, _ = self._rgbw(k)
            self.assertAlmostEqual(r + g, 255, delta=1, msg=k)
            self.assertEqual((r, g), (b, w))
        r, g, _, _, _, _ = self._rgbw(4100)
        self.assertTrue(110 < r < 145 and 110 < g < 145, (r, g))

    def test_special_channel_first_and_footprint(self):
        from scenebuilder import fixtures
        self.assertEqual(fixtures.footprint("TW", 7), 15)
        self.assertEqual(fixtures.render("TW", 7, {"cct": 5000}, special=1)[0], 1)
