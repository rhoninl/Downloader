package main

import (
	"fmt"
)

var (
	sizeUnitMap = []string{"B", "kiB", "MiB", "GiB", "TiB", "PiB"}
	timeUnitMap = []string{"s", "min", "h"}
)

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
