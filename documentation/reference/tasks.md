# Каталог задач

Ниже — карта верхнеуровневых задач текущего AzurPilotRu. Это **справочник имён**, а не рекомендация включать всё сразу.

## Порт

- `Alas` — настройки AzurPilot;
- `General` — общие настройки;
- `Restart` — перезапуск;
- `FleetAutoScan` — автосканирование флотов.

## Боевой выход

- `Main` — основная кампания 1;
- `Main2` — основная кампания 2;
- `Main3` — основная кампания 3;
- `GemsFarming` — срочные комиссии;
- `ThreeOilLowCost` — экономичный флот за 3 нефти;
- `Ambush11` — засада 1-1.

## События

- `EventGeneral` — общие настройки;
- `Event`, `Event2`, `Event3` — карты события;
- `Raid`;
- `RaidScuttle`;
- `Hospital`;
- `Coalition`;
- `CoalitionScuttle`;
- `MaritimeEscort`;
- `EventShop`;
- `WarArchives`.

Ежедневный event-блок:

- `EventA`, `EventB`, `EventC`, `EventD`;
- `EventSp`;
- `RaidDaily`;
- `CoalitionSp`.

## Автосбор наград

- `Commission`;
- `Tactical`;
- `Research`;
- `Dorm`;
- `Meowfficer`;
- `Guild`;
- `Reward`;
- `Awaken`.

## Ежедневные задания

- `Daily`;
- `Hard`;
- `Exercise`;
- `ShopFrequent`;
- `ShopOnce`;
- `Shipyard`;
- `Gacha`;
- `Freebies`;
- `Minigame`;
- `PrivateQuarters`.

## Операция «Сирена»

- `OpsiGeneral`;
- `OpsiAshBeacon`;
- `OpsiAshAssist`;
- `OpsiExplore`;
- `OpsiShop`;
- `OpsiVoucher`;
- `OpsiDaily`;
- `OpsiObscure`;
- `OpsiAbyssal`;
- `OpsiArchive`;
- `OpsiStronghold`;
- `OpsiMonthBoss`;
- `OpsiMeowfficerFarming`;
- `OpsiHazard1Leveling`;
- `OpsiScheduling`;
- `OpsiPreventActionPointOverflow`;
- `OpsiCrossMonth`;
- `OpsiSimulator`.

## План острова

- `IslandPlan`;
- `IslandBusiness`;
- `IslandFarm`;
- `IslandRancher`;
- `IslandMineForest`;
- `IslandRestaurant`;
- `IslandTeahouse`;
- `IslandGrill`;
- `IslandJuuEatery`;
- `IslandJuuCoffee`;
- `IslandManufacture`;
- `IslandDailyGather`;
- `IslandAirDrop`;
- `IslandCargoPreparation`;
- `IslandDailyOrder`;
- `IslandDailyInteract`;
- `IslandPearlSell`.

## Инструменты

- `Daemon`;
- `OpsiDaemon`;
- `EventStory`;
- `BoxDisassemble`;
- `AutoEquip`;
- `Benchmark`;
- `OcrBenchmark`;
- `ScreenshotIntervalBenchmark`;
- `GameManager`;
- `EmulatorManager`.

## Scheduler

Большинство runnable-задач имеют группу `Scheduler`.

Некоторые страницы/конфигурационные блоки, например `Alas`, `General`, `EventGeneral`, `OpsiGeneral`, `IslandPlan` и `OpsiSimulator`, не являются обычными scheduler task в том же смысле.

Практическое описание каждой группы находится в разделе [Задачи автоматизации](../automation/README.md).
