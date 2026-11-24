// waf-blocker 运行在 VM100 旁路由上，封装 ipset，供 waf-console 调用。
//
//	ipset create waf-block hash:ip timeout 3600 maxelem 65536 -exist
//	iptables -I PREROUTING -t raw -m set --match-set waf-block src -j DROP
package main

import (
	"bufio"
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

const set = "waf-block"

func main() {
	token := os.Getenv("BLOCKER_TOKEN")
	listen := os.Getenv("LISTEN")
	if token == "" {
		log.Fatal("BLOCKER_TOKEN required")
	}
	if listen == "" {
		listen = "192.168.3.100:9180" // 仅监听 LAN
	}
	if out, err := exec.Command("ipset", "create", set, "hash:ip", "timeout", "3600", "maxelem", "65536", "-exist").CombinedOutput(); err != nil {
		log.Fatalf("ipset create: %v %s", err, out)
	}
	http.HandleFunc("/v1/blocks", func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) != 1 {
			http.Error(w, "unauthorized", 401)
			return
		}
		switch r.Method {
		case http.MethodGet:
			list(w)
		case http.MethodPost, http.MethodDelete:
			var req struct {
				IP  string `json:"ip"`
				TTL int    `json:"ttl"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024)).Decode(&req) != nil {
				http.Error(w, "bad request", 400)
				return
			}
			ip, err := netip.ParseAddr(req.IP)
			if err != nil || !ip.Is4() { // hash:ip 默认 inet；IPv6 需另建 family inet6 的 set
				http.Error(w, "invalid ipv4", 400)
				return
			}
			args := []string{"del", set, ip.String(), "-exist"}
			if r.Method == http.MethodPost {
				if req.TTL <= 0 || req.TTL > 7*86400 {
					req.TTL = 3600
				}
				args = []string{"add", set, ip.String(), "timeout", strconv.Itoa(req.TTL), "-exist"}
			}
			if out, err := exec.Command("ipset", args...).CombinedOutput(); err != nil {
				http.Error(w, string(out), 500)
				return
			}
			log.Printf("%s %s ttl=%d from=%s", args[0], ip, req.TTL, r.RemoteAddr)
			w.WriteHeader(204)
		default:
			http.Error(w, "method not allowed", 405)
		}
	})
	log.Printf("waf-blocker listening on %s", listen)
	log.Fatal(http.ListenAndServe(listen, nil))
}

// list 解析 `ipset list waf-block`：成员行形如 "1.2.3.4 timeout 3412"
func list(w http.ResponseWriter) {
	out, err := exec.Command("ipset", "list", set).Output()
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	res := map[string]int{}
	sc := bufio.NewScanner(bytes.NewReader(out))
	members := false
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "Members:" {
			members = true
			continue
		}
		if !members || line == "" {
			continue
		}
		f := strings.Fields(line)
		ttl := 0
		if len(f) >= 3 && f[1] == "timeout" {
			ttl, _ = strconv.Atoi(f[2])
		}
		res[f[0]] = ttl
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(res)
}
