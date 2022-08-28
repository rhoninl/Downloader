package main

import (
	"flag"
	"log"
)

func main() {
	url := flag.String("u", "", "downlad file URL, required")
	filename := flag.String("f", "", "save file name, default splite from url")
	blockSize := flag.Int("b", DEFAULT_BLOCK_SIZE, "size of block pre request, default 30 * 1024 = 30 kiB")
	chanBufferSize := flag.Int("bs", DEFAULT_CHAN_BUFFER_SIZE, "size of chan between downloader and filesaver, default 20")
	downloaderGoRuntineNum := flag.Int("dn", DEFAULT_DOWNLOAD_GORUNTINE_NUM, "download goruntine Num, default 20")
	fileWorkerGoRuntineNum := flag.Int("fn", DEFAULT_SAVER_GORUNTINE_NUM, "save file goruntine Num, default 6")
	flag.Parse()
	var config = &managerConfig{
		url:                    *url,
		filename:               *filename,
		blockSize:              *blockSize,
		chanBufferSize:         *chanBufferSize,
		downloaderGoRuntineNum: *downloaderGoRuntineNum,
		fileWorkerGoRuntineNum: *fileWorkerGoRuntineNum,
	}
	manager, err := NewManager(config)
	if err != nil {
		log.Println("error on new manager")
		return
	}
	manager.Run()
}
