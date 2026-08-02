#!/bin/env bash

pprint() { # Pretty print
    echo -e "==> \e[36m${1}\e[39m"
}
prun() { # Pretty run
    pprint "$ ${1}"
    eval " ${1}" || exit 1
}
prun_or_continue() { # Pretty run or continue
    pprint "$ ${1}"
    eval " ${1}" || pprint "↳ Не требуется!"
}

SOURCE="$(dirname $(realpath ${BASH_SOURCE}))" # Poiting to dev_tools folder
CONTAINER="azurlaneautoscript"

# Sanity checks
if [[ "$(id -u)" -eq 0 ]]; then
    pprint "Не запускайте этот скрипт от имени root!"
    exit 1
fi
# Create a lockfile so only one instance of this script can run
if [[ -f "${XDG_RUNTIME_DIR}/${CONTAINER}.lock" ]]; then
    pprint "Этот скрипт уже выполняется!"
    pprint "Если это не так, перезагрузите компьютер!"
    exit 1
else
    touch "${XDG_RUNTIME_DIR}/${CONTAINER}.lock"
fi

# AzurPilot update
pprint "Обновление репозитория"
prun "cd ${SOURCE}/.."
prun "git fetch origin master"
prun "git stash"
prun "git pull --ff origin master"
prun_or_continue "git stash pop"

# Container cleanup
pprint "Завершение предыдущего контейнера"
prun "docker ps | grep ${CONTAINER} | awk '{print \$1}' | xargs -r -n1 docker kill"
pprint "Удаление старых контейнеров"
prun "docker ps -a | grep ${CONTAINER} | awk '{print \$1}' | xargs -r -n1 docker rm"

pprint "Сборка образа"
prun "docker build -t ${CONTAINER} -f ${SOURCE}/Dockerfile ${SOURCE}/../.."

pprint "Завершение серверов ADB на хосте"
prun_or_continue "adb kill-server"

pprint "Запуск контейнера"
trap "rm ${XDG_RUNTIME_DIR}/${CONTAINER}.lock && docker kill ${CONTAINER}" EXIT

prun "docker run --net=host --volume=${SOURCE}/../..:/app/AzurLaneAutoScript:rw --volume=${CONTAINER}-venv:/app/AzurLaneAutoScript/.venv --interactive --tty --name ${CONTAINER} ${CONTAINER}"
# If you need MAA support, uncomment the following two lines and comment the line above(Modify the path of MAA according to the actual situation)
# MAA_SOURCE="${SOURCE}/../../../MAA"
# prun "docker run --net=host --volume=${SOURCE}/..:/app/AzurLaneAutoScript:rw --vloume=${MAA_SOURCE}:/app/MAA:rw --interactive --tty --name ${CONTAINER} ${CONTAINER}"
