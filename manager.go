package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

type manager struct {
	urlInfo

	blockSize              int
	chanBufferSize         int
	fileWorkerGoRuntineNum int
	downloaderGoRuntineNum int
	filename               string
	tmpFilename            string
	file                   *os.File // for controll bitmap
	splits                 chan []int
	wg                     *sync.WaitGroup
	start                  time.Time
}

type managerConfig struct {
	url                    string
	filename               string
	blockSize              int
	chanBufferSize         int
	downloaderGoRuntineNum int
	fileWorkerGoRuntineNum int
}
type urlInfo struct {
	url           string
	allowSplit    bool
	contentLength int
	bitmap
}

type bitmap struct {
	data       []byte
	count      int
	totalCount int
}

type saveData struct {
	data     []byte
	position int
}

const (
	DEFAULT_SAVER_GORUNTINE_NUM     = 8
	DEFAULT_DOWNLOAD_GORUNTINE_NUM  = 20
	DEFAULT_CHAN_BUFFER_SIZE        = 20
	DEFAULT_BLOCK_SIZE              = 30 * 1024 // 30kiB
	DEFAULT_TMP_FILENAME_END_SUFFIX = ".dld"
)

func NewManager(config *managerConfig) (*manager, error) {
	manager := &manager{
		urlInfo: urlInfo{
			url: config.url,
		},
		filename:               config.filename,
		blockSize:              config.blockSize,
		chanBufferSize:         config.chanBufferSize,
		downloaderGoRuntineNum: config.downloaderGoRuntineNum,
		fileWorkerGoRuntineNum: config.fileWorkerGoRuntineNum,
		wg:                     &sync.WaitGroup{},
		start:                  time.Now(),
	}

	// check url is empty
	if manager.url == "" {
		log.Println("please input your url first")
		return nil, errors.New("empty url")
	}

	manager.getInfo()
	return manager, nil
}

func (manager *manager) Run() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if manager.allowSplit {
		manager.spliteDownload(ctx)
	} else {
		fmt.Println("downloading ...  // case single download there were not any schedule")
		manager.singleDownload(ctx)
		fmt.Println("download finished!")
	}
}

func (manager *manager) singleDownload(ctx context.Context) {
	newDownloaderSingle(ctx, manager.url, manager.filename)
}

func (manager *manager) spliteDownload(ctx context.Context) {
	subctx, cancel := context.WithCancel(ctx)
	manager.readBitMap()
	todoChan := manager.splits
	collectorChan := make(chan saveData, manager.chanBufferSize)
	callbackChan := make(chan int, manager.chanBufferSize)
	for i := 0; i < manager.downloaderGoRuntineNum; i++ {
		manager.wg.Add(1)
		go newDownloaderSplite(subctx, manager.url, todoChan, collectorChan, manager.wg)
	}

	for i := 0; i < manager.fileWorkerGoRuntineNum; i++ {
		manager.wg.Add(1)
		go newSaver(subctx, manager.tmpFilename, collectorChan, callbackChan, manager.wg)
	}

	manager.wg.Add(2)
	go manager.progressBar(subctx)
	go manager.newSecretary(subctx, callbackChan)
	for manager.bitmap.totalCount != manager.bitmap.count {
		runtime.Gosched()
	}

	cancel()
	manager.wg.Wait()
}

//getInfo from Url's Header
func (manager *manager) getInfo() error {
	// get some info from header
	resp, err := http.Head(manager.url)
	if err != nil {
		log.Fatalln("cannot get Header From url", err)
	}
	defer resp.Body.Close()
	// check file is allow split
	manager.allowSplit = len(resp.Header["Accept-Ranges"]) != 0 && resp.Header["Accept-Ranges"][0] == "bytes"
	manager.contentLength = int(resp.ContentLength)
	// blind filename from url if filename is not provided
	if manager.filename == "" {
		var urlSplits []string = strings.Split(manager.url, "/")
		manager.filename = strings.Split(urlSplits[len(urlSplits)-1], "?")[0]
	}
	if !manager.allowSplit {
		return nil
	}
	manager.tmpFilename = manager.filename + DEFAULT_TMP_FILENAME_END_SUFFIX
	// check tmpfile is exists
	manager.file, err = os.OpenFile(manager.tmpFilename, os.O_RDWR, 0644)
	if err != nil { // file not exists
		manager.file, err = os.Create(manager.tmpFilename)
		if err != nil {
			log.Println("cannot create tmpFile for download, please check yout access!")
			return err
		}
	}
	return nil
}

func (manager *manager) newBitMap() {
	// cal bytes for bitmap total count
	count := manager.contentLength / manager.blockSize / 8
	manager.bitmap.data = make([]byte, count+1)
	// write bitmap to file's end
	manager.file.WriteAt(manager.bitmap.data, int64(manager.contentLength))

	manager.split()
}

//Splite split content by blockSize
func (manager *manager) split() {
	// finally blockSize
	blockSizeFin := manager.contentLength % manager.blockSize
	// cal total blockSize
	count := manager.contentLength / manager.blockSize
	manager.bitmap.totalCount = count + 1
	var blockSize = manager.blockSize
	// splite content by blockSize and set them to chanBuffer
	result := make(chan []int, count+1)
	var start, end int

	for i := 0; i < count; i++ {
		start = i * blockSize
		end = (i + 1) * blockSize
		result <- []int{start, end - 1}
	}

	result <- []int{end, end + blockSizeFin}

	manager.splits = result
}

// readBitMap read Bitmap From tmpfile
func (manager *manager) readBitMap() {
	info, _ := manager.file.Stat()
	if info.Size() == 0 {
		manager.newBitMap()
	} else {
		manager.praseBitMap()
	}
}

//dropBitMap delete bitmap from file
func (manager *manager) dropBitMap() {
	file, _ := os.OpenFile(manager.tmpFilename, os.O_RDWR, 0644)
	file.Truncate(int64(manager.contentLength)) // resize for delete bitmap
	file.Close()
	os.Rename(manager.tmpFilename, manager.filename)
}

// setBitToMap set single Bit of file
func (manager *manager) setBitToMap(position int) {
	count := position / manager.blockSize / 8
	num := position / manager.blockSize % 8

	manager.bitmap.data[count] |= 1 << num
	// set bit on corresponding location
	manager.file.WriteAt([]byte{manager.bitmap.data[count]}, int64(manager.contentLength+count))

	manager.bitmap.count++
}

// praseBitMap parse Bitmap from bitmap of file
func (manager *manager) praseBitMap() {
	count := manager.contentLength / manager.blockSize
	bitmap := make([]byte, count/8+1)
	manager.bitmap.totalCount = count
	manager.file.ReadAt(bitmap, int64(manager.contentLength))

	var chanInt = make(chan []int, manager.contentLength/manager.blockSize+1)
	bn := manager.contentLength / manager.blockSize

breaker:
	for i, num := 0, 0; i < len(bitmap); i++ {
		for j := 0; j < 8; j++ {
			if num > bn {
				break breaker
			}
			if (bitmap[i]>>j)&1 == 0 {
				end := manager.blockSize*(i*8+j+1) - 1
				if end > manager.contentLength {
					end = manager.contentLength
				}
				chanInt <- []int{manager.blockSize * (i*8 + j), end}
			}
			num++
		}
	}
	manager.count = manager.totalCount - len(chanInt)
	manager.bitmap.data = bitmap
	manager.splits = chanInt
}

func (manager *manager) progressBar(ctx context.Context) {
	defer manager.wg.Done()
	var pert int
	var speed int64
	var initialSize int64 = int64(manager.count * manager.blockSize)
	var size int64 = initialSize
	var lastSize int64 = initialSize
	var download = "download        "
	var tiao = "[__________________________________________________]"
	for i := 0; ; i++ {
		select {
		case <-time.Tick(time.Second):
			tmpDownload := strings.Replace(download, " ", ".", i%4)
			size = int64(manager.count * manager.blockSize)
			speed = size - lastSize
			lastSize = size
			pert = int(size) * 100 / manager.contentLength
			tmpTiao := strings.Replace(strings.Replace(tiao, "_", "=", pert/2), "_", ">", 1)
			fmt.Printf("\t%s\t%s\t%s\t %d%% / 100%% \t%11s/s\t%s               \r", tmpDownload, manager.filename, tmpTiao, pert, sizeUnitSuitable(int(speed)), timeUnitSuitable(float64(i)))
		case <-ctx.Done():
			diff := time.Since(manager.start).Seconds()
			fmt.Println()
			fmt.Println("\t download Success! filename:", manager.filename, "\tfileSize:", sizeUnitSuitable(manager.contentLength))
			log.Printf("\t totalTime: %s\t averageSpeed:%s/s\n", timeUnitSuitable(diff), sizeUnitSuitable((manager.contentLength-int(initialSize))/int(diff)))

			return
		}
	}
}

func (manager *manager) newSecretary(ctx context.Context, callbackChan <-chan int) {
	defer func() {
		manager.dropBitMap()
		manager.file.Close()
		manager.wg.Done()
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case data := <-callbackChan:
			manager.setBitToMap(data)
		}
	}
}
