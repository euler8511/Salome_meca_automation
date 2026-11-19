import salome

salome.standalone()
salome.salome_init()

from salome.geom import geomBuilder

length, width, height = 100.0, 50.0, 30.0

geompy = geomBuilder.New(salome.myStudy)

box = geompy.MakeBoxDXDYDZ(length, width, height)
geompy.addToStudy(box, "ParametricBox")

output_hdf_filename = "C:/Users/ghchoi/Desktop/v2023/output.hdf"

salome.myStudy.SaveAs(output_hdf_filename, False, False)

print(
    f"[Salome Batch Success] 박스 생성 완료 (L={length}, W={width},"
    f" H={height}) -> '{output_hdf_filename}' 저장됨"
)