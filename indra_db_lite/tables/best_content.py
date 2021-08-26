import os
import json
import subprocess
import pandas as pd
from multiprocessing import Pool
from indra_db.util.helpers import unpack
from indra.literature.adeft_tools import universal_extract_paragraphs


from indra_db_lite.csv import query_to_csv


def abstracts_to_csv_raw(outpath: str) -> None:
    """Creates csv file containing abstracts at location specified by path.

    Creates csv file with 5 columns. There is no header but there are columns
    for text_ref_id, text_content_id1, text_content_id2, title, abstract.
    text_ref_id is the id for associated document in indra_db's text_ref
    table, text_content_id1, and text_content_id2 are the ids in indra_db's
    text_content table for the abstract and title of the document respectively.
    The title column contains hex encoded Postgres bytea data for the
    compressed title and the abstract column contains the hex encoded bytea
    data for the compressed abstract.

    Parameters
    ----------
    outpath : str
        Path to location where output file is to be stored.
    """
    abstracts_query = """
    SELECT
        tc1.text_ref_id AS text_ref_id,
        tc1.id AS tc_id1,
        tc2.id AS tc_id2,
        encode(tc2.content, 'hex') AS title,
        encode(tc1.content, 'hex') AS abstract
    FROM
        text_content tc1
    JOIN
        text_content tc2
    ON
        tc1.text_ref_id = tc2.text_ref_id AND
        tc1.text_type = 'abstract' AND
        tc2.text_type = 'title' AND
        tc1.content is NOT NULL AND
        tc2.content is NOT NULL
    """
    query_to_csv(abstracts_query, outpath)


def process_raw_abstracts_csv(
        inpath: str,
        outpath: str,
        chunksize: int = 1000000,
        restart: bool = False,
) -> None:
    """Process csv generated by `abstracts_to_csv_raw` and create new csv.

    Output csv file has 4 columns. There is no header but we will refer to the
    columns in order as text_ref_id, tc_id1, tc_id2, content.  text_ref_id is
    the id for the associated document in indra_db's text_ref table. tc_id1 and
    tc_id2 are the ids for the abstract and title of the document in indra_db's
    text_content table respectively. content contains a json serialized list of
    the form [<title>, <abstract>], where <title> and <abstract> are given as
    plain strings.

    Parameters
    ----------
    inpath : str
        Path to csv file generated by `abstracts_to_csv_raw`.

    outpath : str
        Path to location for placing output file. Overwrites file if it
        already exists at outpath.

    chunksize : Optional[int]
        chunksize parameter passed to `pandas.read_csv` to specify the number
        of rows of the input csv to process at a time to reduce memory
        consumption. Default: 1000000
    """
    if os.path.exists(outpath):
        if restart:
            num_processed_rows = _get_line_count(outpath)
        else:
            # Remove outpath if it already exists. The csv is written to
            # incrementally and the header is only added if the file does
            # not exist yet.
            os.remove(outpath)

    with pd.read_csv(
            inpath,
            sep=',',
            chunksize=chunksize,
            skiprows=range(1, num_processed_rows + 1),
            names=['text_ref_id', 'tc_id1', 'tc_id2', 'title', 'abstract'],
    ) as reader:
        for chunk in reader:
            chunk.loc[:, 'abstract'] = chunk.abstract.apply(
                lambda x: unpack(bytes.fromhex(x))
            )
            chunk.loc[:, 'title'] = chunk.title.apply(
                lambda x: unpack(bytes.fromhex(x))
            )
            chunk.loc[:, 'text_type'] = 'abstract'
            chunk.loc[:, 'content'] = chunk.apply(
                lambda row: json.dumps([row.title, row.abstract]),
                axis=1
            )
            output = chunk[
                ['text_ref_id', 'tc_id1', 'tc_id2', 'text_type', 'content']
            ]
            output.to_csv(
                outpath,
                mode='a',
                header=not os.path.exists(outpath),
                index=False,
            )


def fulltexts_to_csv_raw(outpath: str) -> None:
    """Creates csv file for fulltexts at location specified by outpath.

    Output csv has three columns. There is no header but we refer to the
    columns in order as "text_ref_id", "text_content_id", "fulltext".
    "text_ref_id" contains the id for a document in the text_ref columns in
    indra_db.  "text_content_id" contains the id for the fulltext for this
    document in the text_content table of indra_db. "fulltext" contains the hex
    encoded Postgres bytea data for the compressed fulltext.

    Parameters
    ----------
    outpath : str
        Path where output csv is to be stored.
    """
    fulltexts_query = """
    SELECT
        tc.text_ref_id AS text_ref_id,
        tc.id AS tc_id,
        encode(tc.content, 'hex') AS fulltext
    FROM
        text_content tc
    WHERE
        tc.text_type = 'fulltext' AND
        tc.content IS NOT NULL
    """
    query_to_csv(fulltexts_query, outpath)


def process_raw_fulltexts_csv(
        inpath: str,
        outpath: str,
        chunksize: int = 1000,
        n_jobs: int = 1,
        restart: bool = False,
) -> None:
    """Process csv generated by `fulltexts_to_csv_raw` and generate new csv.

    Output csv has the same columns as the table generated by
    `process_raw_abstracts_csv`. The "content" column contains a json
    serialized list of plaintext paragraphs extracted from the XML for the
    fulltext using `indra.literature.universal_extract_paragraphs`. "tc_id1"
    contains the text_content id for the fulltext. "tc_id2" is set to None,
    since there is no secondary text_content id in this case. This is given so
    that the columns will be identical to those of the abstracts csv generated
    by `process_raw_abstracts_csv`.

    Parameters
    ----------
    inpath : str
        Path to csv file generated by `fulltexts_to_csv_raw`.

    outpath : str
        Path to location for placing output file. Overwrites file if it
        already exists at outpath, unless optional argument restart is set to
        True.

    chunksize : Optional[int]
        chunksize parameter passed to `pandas.read_csv` to specify the number
        of rows of the input csv to process at a time to reduce memory
        consumption. The default is significantly lower than that for
        `process_raw_abstracts_csv` since individual fulltexts can be much
        larger than abstracts. Default: 1000

    restart : Optional[bool]
        If restart is set to True and the file at outpath is in an unfinished
        state, continuing processing and appending to this file until it it
        completed. Useful if an error previously caused processing to halt
        before completing.
    """
    # To allow for restarts, track number of rows that already have been
    # processed.
    num_processed_rows = 0
    if os.path.exists(outpath):
        if restart:
            num_processed_rows = _get_line_count(outpath)
        else:
            # Remove outpath if it already exists. The csv is written to
            # incrementally and the header is only added if the file does
            # not exist yet.
            os.remove(outpath)

    with pd.read_csv(
            inpath,
            sep=',',
            chunksize=chunksize,
            skiprows=range(1, num_processed_rows + 1),
            names=['text_ref_id', 'tc_id1', 'fulltext'],
    ) as reader:
        for chunk in reader:
            with Pool(n_jobs) as pool:
                fulltexts = pool.map(_extract_then_dump, chunk.fulltext)
            # Purge now unneeded column allow garbage collector to reduce
            # memory usage.
            chunk.loc[:, 'fulltext'] = None
            chunk.loc[:, 'content'] = fulltexts
            del fulltexts
            chunk.loc[:, 'tc_id2'] = None
            chunk.loc[:, 'text_type'] = 'fulltext'
            output = chunk[
                ['text_ref_id', 'tc_id1', 'tc_id2', 'text_type', 'content']
            ]
            output.to_csv(
                outpath,
                mode='a',
                header=not os.path.exists(outpath),
                index=False,
            )


def _extract_then_dump(hex_string: str) -> str:
    """Extract."""
    return json.dumps(
        universal_extract_paragraphs(
            unpack(bytes.fromhex(hex_string))
        )
    )


def _get_line_count(path: str) -> int:
    """Get line count for a file."""
    return int(subprocess.check_output(['wc', '-l', path]).split()[0])


def pmid_text_ref_id_to_csv(outpath: str) -> None:
    """Generate csv table mapping pmids to text_ref_ids.

    Generated csv has two columns, the first containing a pmid and their
    second the corresponding id for the associated document in the indra_db's
    text_ref table. The generated table has no headers.

    Parameters
    ----------
    outpath : str
        Path to location where output will be stored.
    """
    pmids_query = """
    SELECT
        pmid, id
    FROM
        text_ref
    WHERE
        pmid is not NULL
    """
    query_to_csv(pmids_query, outpath)
