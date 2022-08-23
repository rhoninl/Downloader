package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

var (
	blockSize   = 30 * 1024 // 30kB
	runtineNum  = 20
	sizeUnitMap = []string{"B", "kiB", "MiB", "GiB", "TiB", "PiB"}
	timeUnitMap = []string{"s", "min", "h"}
)

type HttpDownloader struct {
	url           string
	filename      string
	contentLength int
	acceptRanges  bool
	numThreads    int
	split         chan []int
	currentSize   int
	file          *os.File
	start         time.Time
	locker        *sync.Mutex
	Bitmap
}

type Bitmap struct {
	data []byte
}

func main() {
	url := flag.String("u", "", "downlad file URL")
	runtineNum = *flag.Int("n", runtineNum, "go runtine Num")
	blockSize = *flag.Int("s", blockSize, "blockSize of every request")
	flag.Parse()

	if *url == "" {
		fmt.Println("please set your url first")
		return
	}

	downloader := New(*url, runtineNum)
	downloader.Download()
}

//New 初始化下载器
func New(url string, numThreads int) *HttpDownloader {
	var urlSplits []string = strings.Split(url, "/")
	var filename string = strings.Split(urlSplits[len(urlSplits)-1], "?")[0]

	res, err := http.Head(url)
	if err != nil {
		fmt.Println("error when get resource Head ,error: ", err)
		panic(err)
	}

	downloader := &HttpDownloader{
		url:           url,
		filename:      filename,
		contentLength: int(res.ContentLength),
		numThreads:    numThreads,
		start:         time.Now(),
		acceptRanges:  len(res.Header["Accept-Ranges"]) != 0 && res.Header["Accept-Ranges"][0] == "bytes",
	}

	file, err := os.OpenFile(downloader.filename+".dld", os.O_RDWR, 0644)
	if err != nil {
		file, err = os.Create(downloader.filename + ".dld")
		if err != nil {
			fmt.Println("error when create file")
			panic(err)
		}
	}
	downloader.file = file
	downloader.readBitMap()
	downloader.currentSize = downloader.contentLength - len(downloader.split)*blockSize

	downloader.locker = &sync.Mutex{}
	return downloader
}

func (downloader *HttpDownloader) Download() {
	var wg sync.WaitGroup
	for i := 0; i < downloader.numThreads; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case tmp := <-downloader.split:
					downloader.SplitDownload(tmp[0], tmp[1])
				default:
					return
				}
			}
		}()
	}

	var pert int
	var size, lastSize, speed int64
	var download = "download        "
	var tiao = "[__________________________________________________]"
	for i := 0; pert < 100; i++ {
		select {
		case <-time.Tick(time.Second):
			tmpDownload := strings.Replace(download, " ", ".", i%4)
			size, speed, lastSize = int64(downloader.currentSize), size-lastSize, size
			pert = int(size) * 100 / downloader.contentLength
			tmpTiao := strings.Replace(strings.Replace(tiao, "_", "=", pert/2), "_", ">", 1)
			fmt.Printf("\t%s\t%s\t%s\t %d%% / 100%% \t%11s/s\t%s               \r", tmpDownload, downloader.filename, tmpTiao, pert, sizeUnitSuitable(int(speed)), timeUnitSuitable(float64(i)))
		}
	}
	wg.Wait()
	downloader.deleteBitMap()

	err := os.Rename(downloader.filename+".dld", downloader.filename)
	log.Println(err)
	diff := time.Since(downloader.start).Seconds()
	fmt.Println()
	fmt.Println("\t download Success! filename:", downloader.file.Name(), "\tfileSize:", sizeUnitSuitable(downloader.contentLength))
	log.Printf("\t totalTime: %s\t averageSpeed:%s/s\n", timeUnitSuitable(diff), sizeUnitSuitable(downloader.contentLength/int(diff)))
}

//SplitDownload 文件分片下载
func (downloader *HttpDownloader) SplitDownload(start, end int) {
	req, err := http.NewRequest(http.MethodGet, downloader.url, nil)
	if err != nil {
		fmt.Println("[SplitDownload]构建请求时发生错误！")
		log.Panicln(err)
	}

	req.Header.Set("Range", fmt.Sprintf("bytes=%v-%v", start, end))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Println("[SplitDownload]发送请求时发生错误！")
		log.Panicln(err)
	}

	downloader.SaveFile(downloader.file, int64(start), resp)
	downloader.setBitMap(start)
	downloader.currentSize += blockSize
}

func (downloader *HttpDownloader) SaveFile(file *os.File, offset int64, resp *http.Response) {
	downloader.locker.Lock()
	defer downloader.locker.Unlock()
	file.Seek(offset, 0)

	content, err := io.ReadAll(resp.Body)
	if err != nil {
		fmt.Println("error when write bytes to file, error: ", err)
		panic(err)
	}
	defer resp.Body.Close()
	file.Write(content)
	file.Sync()
}

//Split 将下载文件分段
func (downloader *HttpDownloader) Split() {
	blockSizeFin := downloader.contentLength % blockSize
	count := downloader.contentLength/blockSize + 1

	result := make(chan []int, count)
	var start, end int
	for i := 0; i < count-1; i++ {
		start = i * blockSize
		end = (i + 1) * blockSize
		result <- []int{start, end - 1}
	}
	result <- []int{end, end + blockSizeFin}
	downloader.split = result
}

func sizeUnitSuitable(data int) string {
	i := 0
	var dataF = float64(data)

	for i = 0; dataF > 1024; i++ {
		dataF /= 1024
	}

	return fmt.Sprintf("%.2f %s", dataF, sizeUnitMap[i])
}

func timeUnitSuitable(data float64) string {
	i := 0
	dataF := data

	for i = 0; dataF > 60; i++ {
		dataF /= 60
	}

	zheng := int(dataF)
	xiao := int((dataF - float64(zheng)) * 100)

	if i == 0 {
		return fmt.Sprintf("%02d %s", zheng, timeUnitMap[i])
	}

	return fmt.Sprintf("%d:%02d %s", zheng, xiao, timeUnitMap[i])
}

func (downloader *HttpDownloader) NewBitMap() {
	count := ((downloader.contentLength+1)/blockSize + 1) / 8
	downloader.Bitmap.data = make([]byte, count)
	downloader.file.WriteAt(downloader.Bitmap.data, int64(downloader.contentLength))
	downloader.Split()
}

func (downloader *HttpDownloader) readBitMap() {
	info, _ := downloader.file.Stat()
	if info.Size() == 0 {
		downloader.NewBitMap()
	} else {
		downloader.praseBitMap()
	}
}

func (downloader *HttpDownloader) praseBitMap() {
	count := (downloader.contentLength + 1) / blockSize
	bitmap := make([]byte, count)
	downloader.file.ReadAt(bitmap, int64(downloader.contentLength))

	var chanInt = make(chan []int, downloader.contentLength/blockSize+1)
	bn := downloader.contentLength / blockSize

breaker:
	for i, num := 0, 0; i < len(bitmap); i++ {
		for j := 0; j < 8; j++ {
			if num > bn {
				break breaker
			}
			if (bitmap[i]>>j)&1 == 0 {
				end := blockSize*(i*8+j+1) - 1
				if end > downloader.contentLength {
					end = downloader.contentLength
				}
				chanInt <- []int{blockSize * (i*8 + j), end}
			}
			num++
		}
	}
	downloader.split = chanInt
}

// postion is the start
func (downloader *HttpDownloader) setBitMap(position int) {
	var bitmap = make([]byte, 1)
	count := position / blockSize / 8
	num := position / blockSize % 8

	downloader.file.ReadAt(bitmap, int64(downloader.contentLength+count))

	bitmap[0] = bitmap[0] | (1 << num)

	downloader.file.WriteAt(bitmap, int64(downloader.contentLength+count))
}

func (downloader *HttpDownloader) deleteBitMap() {
	downloader.file.Truncate(int64(downloader.contentLength))
}
