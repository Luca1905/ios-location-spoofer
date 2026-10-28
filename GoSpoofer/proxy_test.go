package main

import (
	"bytes"
	"encoding/binary"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/elazarl/goproxy"
	pb "golocationspoofer/pb"
	"google.golang.org/protobuf/proto"
)

func wifiLookupRequest(t *testing.T) []byte {
	t.Helper()
	payload, err := proto.Marshal(&pb.AppleWLoc{WifiDevices: []*pb.WifiDevice{{Bssid: "aa:bb:cc:dd:ee:ff"}}})
	if err != nil {
		t.Fatal(err)
	}
	return ArpcSerialize(&ArpcRequest{Version: "1", Locale: "en-001_001", AppIdentifier: "com.apple.locationd", OsVersion: "27.0.1", FunctionId: 2, Payload: payload})
}

func TestUnsupportedRequestPreservesBody(t *testing.T) {
	body := []byte("unsupported Apple request")
	req := httptest.NewRequest(http.MethodPost, "https://gs-loc.apple.com/clls/wloc", bytes.NewReader(body))
	forwarded, response := handleLocationRequest(req)
	if response != nil {
		t.Fatal("unsupported request must pass through")
	}
	got, err := io.ReadAll(forwarded.Body)
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("forwarded body = %q, error = %v; want %q", got, err, body)
	}
}

func TestEmptyWifiLookupPassesThrough(t *testing.T) {
	body := ArpcSerialize(&ArpcRequest{Version: "1", Locale: "en", AppIdentifier: "com.apple.locationd", OsVersion: "27", FunctionId: 2, Payload: []byte{0x20, 0}})
	req := httptest.NewRequest(http.MethodPost, "https://gs-loc.apple.com/clls/wloc", bytes.NewReader(body))
	forwarded, response := handleLocationRequest(req)
	if response != nil {
		t.Fatal("a lookup without access points must not fabricate an empty success response")
	}
	got, err := io.ReadAll(forwarded.Body)
	if err != nil || !bytes.Equal(got, body) {
		t.Fatal("original request was not preserved")
	}
}

func TestWifiResponseContainsUsableLocation(t *testing.T) {
	spoofLat, spoofLon = 53.631269, 9.999210
	req := httptest.NewRequest(http.MethodPost, "https://gs-loc.apple.com/clls/wloc", bytes.NewReader(wifiLookupRequest(t)))
	_, response := handleLocationRequest(req)
	if response == nil || response.StatusCode != http.StatusOK {
		t.Fatal("missing spoofed response")
	}
	data, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) < 10 || int(binary.BigEndian.Uint16(data[8:10])) != len(data)-10 {
		t.Fatalf("invalid response framing: %x", data)
	}
	var decoded pb.AppleWLoc
	if err := proto.Unmarshal(data[10:], &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.WifiDevices) != 1 {
		t.Fatal("Wi-Fi lookup lost access points")
	}
	location := decoded.WifiDevices[0].GetLocation()
	if location == nil || location.GetLatitude() != IntFromCoord(spoofLat) || location.GetLongitude() != IntFromCoord(spoofLon) {
		t.Fatalf("wrong coordinates: %v", location)
	}
	if location.HorizontalAccuracy == nil || location.GetHorizontalAccuracy() <= 0 {
		t.Fatalf("response has no usable horizontal accuracy: %v", location)
	}
}

func TestLocationHostWithExplicitPort(t *testing.T) {
	proxy := goproxy.NewProxyHttpServer()
	proxy.OnRequest().DoFunc(func(req *http.Request, ctx *goproxy.ProxyCtx) (*http.Request, *http.Response) {
		ctx.RoundTripper = goproxy.RoundTripperFunc(func(r *http.Request, _ *goproxy.ProxyCtx) (*http.Response, error) {
			return goproxy.NewResponse(r, "text/plain", http.StatusTeapot, "unexpected passthrough"), nil
		})
		return req, nil
	})
	setupLocationSpoofing(proxy)
	recorder := httptest.NewRecorder()
	proxy.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "http://gs-loc.apple.com:443/clls/wloc", bytes.NewReader(wifiLookupRequest(t))))
	if recorder.Code != http.StatusOK {
		t.Fatalf("location lookup with explicit port missed handler: status %d", recorder.Code)
	}
}
